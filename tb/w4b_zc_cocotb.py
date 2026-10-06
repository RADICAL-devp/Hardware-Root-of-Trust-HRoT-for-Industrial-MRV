"""Week 4b cocotb: zc_detect vs sensors/windows.py golden (toplevel=zc_detect).

Proves the detector contract on the strobe TIMING side: cross_strobe /
win_start_pulse / win_end_strobe fire during exactly the golden detection
cycles, M == E - S by counter subtraction, win_valid folds gap [150,260]
and M [1810,2230] range checks. Window [S,E) sample INCLUSION (sums) is
proven at the power level (w4b_power_cocotb), which replays these strobes;
the shared synth_cross_stream vectors keep both sides consistent.
"""

import cocotb
import numpy as np
from cocotb.triggers import RisingEdge

from sensors.adc import ADCParams, quantize_voltage
from sensors.windows import ZCParams, build_windows, find_rising_crossings_q15
from tb.w4b_common import mask16, reset_dut, settle, start_clock, synth_cross_stream

ZC = ZCParams()
BASE_CTR = 1_000_000  # nonzero counter base: no hardcoded-zero assumption


async def drive_stream(dut, v_codes, base=BASE_CTR):
    """Drive codes; return (cross_cycles, starts, ends) with M/valid at ends.

    Strobes are combinational: inputs set, 1 ns settle, read BEFORE the edge.
    """
    cross, starts, ends = [], [], []
    dut.sample_valid.value = 0
    for k, code in enumerate(v_codes):
        dut.sample_valid.value = 1
        dut.v_q15.value = mask16(code)
        dut.i_q15.value = 0
        dut.counter_in.value = base + k
        await settle()
        c = int(dut.cross_strobe.value)
        ws = int(dut.win_start_pulse.value)
        we = int(dut.win_end_strobe.value)
        if c:
            cross.append(k)
        if ws:
            starts.append(k)
        if we:
            ends.append((k, int(dut.M.value), int(dut.win_valid.value)))
        await RisingEdge(dut.clk)
    dut.sample_valid.value = 0
    await RisingEdge(dut.clk)
    return cross, starts, ends


def check_stream(dut_cross, dut_starts, dut_ends, codes):
    """Match a driven stream against the golden detector + window builder."""
    arr = np.array(codes, dtype=np.int64)
    det, frac = find_rising_crossings_q15(arr)
    assert dut_cross == [int(d) for d in det], (
        f"detections {dut_cross} != golden {[int(d) for d in det]}"
    )
    wins = build_windows(det, frac, len(codes), ZC)
    exp_starts = [int(det[j]) for j in range(0, det.shape[0] - ZC.n_cycles, ZC.n_cycles)]
    # Every window end also OPENS the next window (E is owned by it, so the
    # accumulator must start there) — hence one trailing win_start_pulse the
    # golden window list does not emit. Expected starts = golden starts +
    # the last golden end (or the lone first detection if no window closed).
    if wins:
        exp_starts = exp_starts + [wins[-1].end_idx]
    elif det.shape[0]:
        exp_starts = [int(det[0])]
    assert dut_starts == exp_starts, f"starts {dut_starts} != {exp_starts}"
    assert len(dut_ends) == len(wins)
    for (e, m, vv), w in zip(dut_ends, wins):
        assert e == w.end_idx, f"end {e} != {w.end_idx}"
        assert m == w.n_samples, f"M {m} != {w.n_samples}"
        assert vv == (1 if w.valid else 0), f"valid {vv} != {w.valid} at end {e}"
    return wins


def sine_stream(f_hz=50.0, secs=0.7, vpk=325.0, seed=42, noise=0.35):
    """Measured-style sine codes [counts]: 12-bit-quantized + noise."""
    rng = np.random.default_rng(seed)
    n = int(round(secs * 10_000))
    t = np.arange(n) / 10_000.0
    v = quantize_voltage(vpk * np.sin(2 * np.pi * f_hz * t) + rng.normal(0, noise, n), ADCParams())
    return [max(-32768, min(32767, int(round(x / 500.0 * 32768.0)))) for x in v]


@cocotb.test()
async def test_zc_valid_windows(dut):
    """Noisy 50 Hz sine: strobes/M/valid match golden on every window."""
    start_clock(dut)
    await reset_dut(dut)
    codes = sine_stream()
    cross, starts, ends = await drive_stream(dut, codes)
    wins = check_stream(cross, starts, ends, codes)
    assert len(wins) >= 2 and all(w.valid for w in wins)
    for _, m, _ in ends:
        assert ZC.m_min_samples <= m <= ZC.m_max_samples
    cocotb.log.info(f"valid windows: {len(wins)} windows, Ms={[m for _, m, _ in ends]}")


@cocotb.test()
async def test_zc_boundaries(dut):
    """[S,E) boundary indices exact on the shared power-record stream."""
    start_clock(dut)
    await reset_dut(dut)
    det = [2 + k * 200 for k in range(51)]
    codes = synth_cross_stream(det[-1] + 4, det)
    cross, starts, ends = await drive_stream(dut, codes)
    wins = check_stream(cross, starts, ends, codes)
    assert [w.start_idx for w in wins] == det[0:50:10]
    assert [w.end_idx for w in wins] == det[10:51:10]
    assert [w.n_samples for w in wins] == [2000] * 5
    # Shared-sample ownership: E of window k IS S of window k+1 — one strobe
    # cycle carries both win_end and the next win_start (incl. the trailing
    # start at d50, which opens the uncompleted 6th window). The power record
    # test re-drives this exact stream and proves sum exclusion/inclusion.
    for k in range(5):
        assert starts[k + 1] == ends[k][0]
    cocotb.log.info("boundaries: 5 windows [S,E) exact, shared samples owned by next")


@cocotb.test()
async def test_zc_arm_threshold(dut):
    """Arm threshold pinned to ±1 code: strict < -328 arms, >= -328 does not.

    Five dip attempts (lo, lo, +100): -300, -320, -328 must NOT arm (no
    detection); -329 and -350 must arm (detection). Catches any drift of
    ARM_Q15 in either direction.
    """
    start_clock(dut)
    await reset_dut(dut)
    los = [-300, -320, -328, -329, -350]
    det_hi = [100 + k * 100 for k in range(5)]
    length = det_hi[-1] + 4
    codes = [5000] * length
    for d, lo in zip(det_hi, los):
        codes[d - 2] = lo
        codes[d - 1] = lo
        codes[d] = 100
    cross, starts, ends = await drive_stream(dut, codes)
    assert cross == det_hi[3:], f"threshold moved: detections {cross}"
    # Two detections, no closed window: lone first-detection start only.
    assert starts == [det_hi[3]] and ends == []
    cocotb.log.info("arm threshold: -328 silent, -329 arms (±1 code exact)")


@cocotb.test()
async def test_zc_m_edges(dut):
    """M = 1809/1810/2230/2231: range edges exact, gaps legal throughout."""
    start_clock(dut)
    await reset_dut(dut)
    for step, total, want_valid in ((181, 1810, 1), (223, 2230, 1)):
        det = [2 + k * step for k in range(11)]
        codes = synth_cross_stream(det[-1] + 4, det)
        cross, starts, ends = await drive_stream(dut, codes)
        assert cross == det, f"step {step}: detections moved"
        assert len(ends) == 1
        assert ends[0][1] == total, f"M {ends[0][1]} != {total}"
        assert ends[0][2] == want_valid
        await reset_dut(dut)
    # Just outside: M = 1809 (nine 181-gaps + 180) and 2231 (nine 223 + 224).
    det = [2 + k * 181 for k in range(10)] + [2 + 9 * 181 + 180]
    codes = synth_cross_stream(det[-1] + 4, det)
    cross, starts, ends = await drive_stream(dut, codes)
    assert len(ends) == 1 and ends[0][1] == 1809 and ends[0][2] == 0
    await reset_dut(dut)
    det = [2 + k * 223 for k in range(10)] + [2 + 9 * 223 + 224]
    codes = synth_cross_stream(det[-1] + 4, det)
    cross, starts, ends = await drive_stream(dut, codes)
    assert len(ends) == 1 and ends[0][1] == 2231 and ends[0][2] == 0
    cocotb.log.info("M edges: 1810/2230 valid, 1809/2231 invalid, all exact")


@cocotb.test()
async def test_zc_failure_modes(dut):
    """Dropout (missing), glitch (extra), sag (none), freq step: all match."""
    start_clock(dut)
    await reset_dut(dut)
    # Missing crossing: zeroed dropout swallows crossings -> gap invalid.
    codes = sine_stream(secs=2.0)
    codes[int(0.5 * 10_000) : int(0.7 * 10_000)] = [0] * int(0.2 * 10_000)
    cross, starts, ends = await drive_stream(dut, codes)
    wins = check_stream(cross, starts, ends, codes)
    bad = [w for w in wins if not w.valid]
    assert bad and all(w.reason == "gap" for w in bad)
    await reset_dut(dut)
    # Extra crossing: deep single-sample glitch mid-positive-half.
    codes = sine_stream(secs=1.0)
    k0 = 5050
    assert codes[k0 - 1] > 0 and codes[k0] > 0
    codes[k0] = -26214  # q15(-400 V): arms, then spurious trigger
    cross, starts, ends = await drive_stream(dut, codes)
    wins = check_stream(cross, starts, ends, codes)
    assert any(not w.valid for w in wins if w.start_idx <= k0 < w.end_idx)
    await reset_dut(dut)
    # Sag below hysteresis: no arm, no crossings, no windows at all.
    codes = sine_stream(vpk=3.0)
    cross, starts, ends = await drive_stream(dut, codes)
    assert cross == [] and starts == [] and ends == []
    await reset_dut(dut)
    # Frequency step mid-stream: spanning window invalid, matches golden.
    seg1 = sine_stream(f_hz=50.0, secs=1.0, seed=42)
    seg2 = sine_stream(f_hz=30.0, secs=1.0, seed=43)
    codes = seg1 + seg2
    cross, starts, ends = await drive_stream(dut, codes)
    wins = check_stream(cross, starts, ends, codes)
    spanning = [w for w in wins if w.start_idx < len(seg1) < w.end_idx]
    assert spanning and all(not w.valid for w in spanning)
    cocotb.log.info("failure modes: dropout/glitch/sag/step all match golden")


@cocotb.test()
async def test_zc_reset_mid_window(dut):
    """Reset mid-window: no spurious strobes, clean restart after."""
    start_clock(dut)
    await reset_dut(dut)
    codes = sine_stream(secs=0.5)
    # Drive partway, then reset WITH trigger-shaped inputs held up.
    for k in range(1000):
        dut.sample_valid.value = 1
        dut.v_q15.value = codes[k] & 0xFFFF
        dut.i_q15.value = 0
        dut.counter_in.value = BASE_CTR + k
        await settle()
        await RisingEdge(dut.clk)
    dut.rst_n.value = 0
    for _ in range(3):
        dut.sample_valid.value = 1
        dut.v_q15.value = 100 & 0xFFFF  # would trigger if armed (it is not)
        dut.i_q15.value = 0
        dut.counter_in.value = BASE_CTR + 9999
        await settle()
        assert int(dut.cross_strobe.value) == 0
        assert int(dut.win_end_strobe.value) == 0
        assert int(dut.win_start_pulse.value) == 0
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    # Fresh synthetic window after reset: detection indices restart cleanly.
    det = [2 + k * 200 for k in range(11)]
    codes2 = synth_cross_stream(det[-1] + 4, det)
    cross, starts, ends = await drive_stream(dut, codes2, base=5000)
    assert cross == det
    assert len(ends) == 1 and ends[0][1] == 2000 and ends[0][2] == 1
    cocotb.log.info("reset: silent under reset, exact windows after")
