"""Week 5b cocotb: zc_detect -> power_calc chain (toplevel=zc_power_chain).

Detector-driven overrun: a spurious-crossing storm closes windows every
~44 samples (gap 4 << 150, invalid) while finalization needs 401 cycles,
so nearly every win_end drops to a tombstone. Then clean sine windows
must verify byte-exact again (spans from monitored win_end counters).
Invariants: EVERY win_end yields exactly one out_valid (data or
tombstone); expected drops come from the busy rule (drop iff the win_end
edge falls strictly inside a [start, start+400] busy interval —
coincident-with-completion restarts instead); the overrun counter is
pinned to the modeled drop count exactly.
"""

import cocotb
from cocotb.triggers import RisingEdge

from tb.golden import window_power
from tb.w4b_common import mask16, read_sig, reset_dut, settle, sine_codes, start_clock

LAT = 401  # [cycles] inclusive win_end -> out_valid (DECISIONS.md)
ARM_LO = -500  # [counts] arming dip (< ARM_Q15 = -328)
CROSS_HI = 100  # [counts] crossing level (>= 0)
BASE_V = 5000  # [counts] quiet level (never arms, never triggers)


async def monitor(dut, log, stop_ev, state):
    """Unused (kept for symmetry): this test steps inline (see below)."""
    while not stop_ev.is_set():
        await RisingEdge(dut.clk)
        await settle()
        state["edge"] += 1


@cocotb.test()
async def test_chain_spurious_overrun(dut):
    """Spurious storm -> tombstones + exact counter; clean sine recovers exact."""
    # Inline stepping (no concurrent monitor): zc_detect's strobes are
    # combinational on registered count state, so win_end must be sampled
    # BEFORE the advancing edge (post-edge the count already moved on);
    # out_valid is registered, sampled AFTER the edge. `edge` counts clock
    # edges from here on; sample counter k advances per sample.
    start_clock(dut)
    dut.cnt_clear.value = 0
    await reset_dut(dut)
    await settle()
    log: list = []
    edge = 0
    k = 0

    def poll_win_end():
        if read_sig(dut.win_end_strobe, 1):
            log.append(
                (
                    "win_end",
                    edge + 1,
                    int(dut.counter_in.value),
                    read_sig(dut.win_M, 12),
                    read_sig(dut.win_valid, 1),
                )
            )

    def poll_out():
        if read_sig(dut.out_valid, 1):
            log.append(
                (
                    "out",
                    edge,
                    read_sig(dut.p_avg_exact, 32, signed=True),
                    read_sig(dut.vrms_q15, 16),
                    read_sig(dut.irms_q15, 16),
                    read_sig(dut.pf_q15, 16, signed=True),
                    read_sig(dut.energy_uwh, 32, signed=True),
                    read_sig(dut.m_out, 12),
                    int(dut.valid_out.value),
                    read_sig(dut.p_sum_q30, 64, signed=True),
                    read_sig(dut.finalize_overrun_cnt, 16),
                    read_sig(dut.finalize_overrun, 1),
                )
            )

    async def sample(v, i):
        nonlocal edge, k
        dut.sample_valid.value = 1
        dut.v_q15.value = mask16(v)
        dut.i_q15.value = mask16(i)
        dut.counter_in.value = k
        k += 1
        await settle()
        poll_win_end()
        await RisingEdge(dut.clk)
        edge += 1
        await settle()
        dut.sample_valid.value = 0
        poll_out()

    async def idle(n):
        nonlocal edge
        dut.sample_valid.value = 0
        for _ in range(n):
            await RisingEdge(dut.clk)
            edge += 1
            await settle()
            poll_out()

    # Phase 1: period-4 [ARM, ARM, CROSS, BASE] -> detection every 4
    # samples -> win_end every 44 samples << 401: overrun storm.
    for _ in range(150):  # 600 samples
        await sample(ARM_LO, 1000)
        await sample(ARM_LO, 1000)
        await sample(CROSS_HI, 1000)
        await sample(BASE_V, 1000)
    # Phase 2: drain (no detections possible, finalizations finish).
    for _ in range(500):
        await sample(BASE_V, 1000)
    # Phase 3: clean sine recovery.
    vs, isi = sine_codes(8000, seed=99)
    for vv, ii in zip(vs, isi):
        await sample(vv, ii)
    await idle(450)  # last window needs the full schedule

    spur_v = [ARM_LO, ARM_LO, CROSS_HI, BASE_V] * 150
    all_v = spur_v + [BASE_V] * 500 + [int(x) for x in vs]
    all_i = [1000] * 600 + [1000] * 500 + [int(x) for x in isi]

    win_ends = [r for r in log if r[0] == "win_end"]
    outs = [r for r in log if r[0] == "out"]
    assert len(win_ends) >= 12, f"spurious storm produced {len(win_ends)} win_ends"
    # Every win_end yields exactly one out_valid (data or tombstone).
    assert len(outs) == len(win_ends), (len(outs), len(win_ends))

    # Expected drops from the busy rule: drop iff the win_end edge falls
    # strictly inside a [start, start+400] busy interval (coincident with
    # the completion edge restarts instead — the idle-tie rule).
    busy_until = -(10**18)
    exp_drops = exp_starts = 0
    for _, e, _, _, _ in win_ends:
        if e < busy_until:
            exp_drops += 1
        else:
            exp_starts += 1
            busy_until = e + LAT - 1
    assert exp_drops >= 5, "storm too gentle to overrun"
    assert len(outs) == exp_drops + exp_starts
    assert outs[-1][10] == exp_drops, f"overrun {outs[-1][10]} != modeled {exp_drops}"
    assert outs[-1][11] == (1 if exp_drops else 0)

    # Recovery: valid win_ends fully inside the sine section; the 2nd and
    # 3rd such windows check byte-exact (the 1st may straddle the drain).
    # Valid data outputs arrive in window order: match by position.
    sine_base = 600 + 500
    rec = [w for w in win_ends if w[2] - 2000 >= sine_base and w[4] == 1]
    datas = [r for r in outs if r[8] == 1]
    assert len(rec) >= 3 and len(datas) >= 3, (len(rec), len(datas))
    for j in (1, 2):
        s = rec[j - 1][2]
        e = rec[j][2]
        assert rec[j][3] == e - s, "M must equal the counter span"
        g = window_power(all_v[s:e], all_i[s:e])
        o = datas[j]  # valid outputs arrive in window order
        assert (o[2], o[3], o[4], o[5], o[6], o[9]) == (
            g.p_avg_q30,
            g.vrms_q15,
            g.irms_q15,
            g.pf_q15,
            g.energy_uwh_inc,
            g.p_sum_q30,
        ), f"recovery window {j} mismatch"
    # No new drops during recovery: counter frozen since the storm.
    assert outs[-1][10] == exp_drops
    cocotb.log.info(f"chain: {exp_drops} modeled drops exact, 2 recovery windows exact")
