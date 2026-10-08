"""Week 5b cocotb: power_calc FSM finalization behavior (toplevel=power_calc).

Proves the 401-cycle schedule beyond exactness: real-rate operation (1200
clocks/sample), overrun drops with tombstones + recovery, [S,E) sample
ownership at both rates (incl. a full-scale E on a dropped window),
win_end on the cycle before/on/after out_valid, and clean streams at
minimum valid spacing. All data outputs stay byte-exact vs tb/golden.py
window_power; latencies equal 401 everywhere (overrun tombstones excepted:
1 cycle after the dropped win_end).
"""

import cocotb
from cocotb.triggers import RisingEdge, Timer

from tb.golden import window_power
from tb.w4b_common import mask16, read_sig, reset_dut, settle, start_clock

LAT = 401  # [cycles] inclusive win_end-edge -> out_valid-edge (DECISIONS.md)
REAL_CPS = 1200  # [clocks/sample] real 12 MHz / 10 kHz ratio
CLK_NS = 10  # [ns] testbench clock period


def edge_now_ns() -> int:
    """Current sim time [ns] (call right after a RisingEdge)."""
    from cocotb.utils import get_sim_time

    return int(get_sim_time("ns"))


async def feed_window(dut, v, i, m_port, valid, cps=1, e_v=12345, e_i=-2345):
    """Drive n samples + E sample at cps clocks/sample; return win_end ns.

    Strobes mimic zc_detect [S,E) timing: win_start WITH S, win_end WITH E
    (E excluded from this window, owned by the next). No waiting after.
    """
    n = len(v)
    assert len(i) == n
    sv, vv, ii = dut.sample_valid, dut.v_q15, dut.i_q15
    ws, we = dut.win_start_pulse, dut.win_end_strobe
    gap_ns = (cps - 1) * CLK_NS
    for k in range(n):
        sv.value = 1
        vv.value = mask16(v[k])
        ii.value = mask16(i[k])
        ws.value = 1 if k == 0 else 0
        we.value = 0
        dut.M.value = 0
        dut.win_valid_in.value = 0
        await RisingEdge(dut.clk)
        await settle()
        sv.value = 0
        ws.value = 0
        if gap_ns:
            await Timer(gap_ns, unit="ns")
    sv.value = 1
    vv.value = mask16(e_v)
    ii.value = mask16(e_i)
    ws.value = 0
    we.value = 1
    dut.M.value = m_port
    dut.win_valid_in.value = 1 if valid else 0
    await RisingEdge(dut.clk)
    t_we = edge_now_ns()
    sv.value = 0
    we.value = 0
    dut.M.value = 0
    return t_we


async def await_out(dut, timeout=600, fresh=True):
    """Wait for out_valid; return full readout incl. overrun state.

    Checks the CURRENT (post-settle) value first: an out_valid pulse can
    coincide with the feed end (e.g. a completion on the same edge as the
    next window's win_end), and waiting for the next edge would miss it.
    fresh=True after a feed (a coincident pulse is wanted); fresh=False
    chained after a previous await_out (advances one edge first so the
    just-consumed pulse is not caught twice).
    """
    if not fresh:
        await RisingEdge(dut.clk)
    for _ in range(timeout + 1):
        await settle()
        if int(dut.out_valid.value):
            return {
                "t_ns": edge_now_ns(),
                "p_avg_exact": read_sig(dut.p_avg_exact, 32, signed=True),
                "p_avg_lut": read_sig(dut.p_avg_lut, 32, signed=True),
                "vrms": read_sig(dut.vrms_q15, 16),
                "irms": read_sig(dut.irms_q15, 16),
                "pf": read_sig(dut.pf_q15, 16, signed=True),
                "energy": read_sig(dut.energy_uwh, 32, signed=True),
                "m": read_sig(dut.m_out, 12),
                "valid": int(dut.valid_out.value),
                "p_sum": read_sig(dut.p_sum_q30, 64, signed=True),
                "ov": read_sig(dut.finalize_overrun_cnt, 16),
                "ov_flag": read_sig(dut.finalize_overrun, 1),
            }
        await RisingEdge(dut.clk)
    raise AssertionError("out_valid never pulsed")


def check_exact(out, v, i, m_port):
    """All data fields byte-exact vs window_power (M == sample count)."""
    assert len(v) == m_port
    g = window_power([int(x) for x in v], [int(x) for x in i])
    assert out["m"] == m_port == g.m
    assert out["p_sum"] == g.p_sum_q30
    assert out["p_avg_exact"] == g.p_avg_q30
    assert out["vrms"] == g.vrms_q15
    assert out["irms"] == g.irms_q15
    assert out["pf"] == g.pf_q15
    assert out["energy"] == g.energy_uwh_inc
    assert out["valid"] == 1


@cocotb.test()
async def test_fsm_real_rate(dut):
    """~8 windows at the real 1200 clocks/sample: exact + 401 each.

    M=48 with TB-driven win_valid_in=1 is OUTSIDE the zc range by
    construction: this pins finalization rate-independence, not detection.
    Codes are nonzero throughout (full datapath exercised, p_avg != 0).
    """
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    import numpy as np

    rng = np.random.default_rng(52)
    m = 48
    for w in range(8):
        v = [int(x) for x in rng.integers(-30000, 30001, size=m)]
        i = [int(x) for x in rng.integers(-15000, 15001, size=m)]
        assert any(x != 0 for x in v) and any(x != 0 for x in i)
        t_we = await feed_window(dut, v, i, m, True, cps=REAL_CPS)
        out = await await_out(dut)
        assert (out["t_ns"] - t_we) // CLK_NS + 1 == LAT, f"window {w}"
        check_exact(out, v, i, m)
        assert out["p_avg_exact"] != 0, f"window {w}: datapath idle?"
        assert (out["ov"], out["ov_flag"]) == (0, 0)
    cocotb.log.info("real-rate: 8 windows exact, 401 cycles each")


@cocotb.test()
async def test_fsm_overrun_recovery(dut):
    """Drop mid-finalization: tombstone, sticky flag, exact recovery.

    A (M=2000) starts; B ends 200 cycles later (busy -> DROP: tombstone 1
    cycle after B's win_end, overrun 0->1, flag set); C ends idle (exact).
    A's own out_valid still lands at T0+401: drops never disturb the
    in-flight finalization.
    """
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    vA = [10000] * 2000
    iA = [5000] * 2000
    t0 = await feed_window(dut, vA, iA, 2000, True)
    vB = [100] * 200
    iB = [50] * 200
    tB = await feed_window(dut, vB, iB, 200, True)
    assert (tB - t0) // CLK_NS == 200 + 1  # E-cycle each (+1: we presents E)
    tomb = await await_out(dut)
    assert (tomb["t_ns"] - tB) // CLK_NS + 1 == 2, "tombstone 1 cycle after drop"
    for k in ("p_avg_exact", "p_avg_lut", "vrms", "irms", "pf", "energy", "m", "p_sum"):
        assert tomb[k] == 0, f"tombstone leaks {k}"
    assert tomb["valid"] == 0
    assert (tomb["ov"], tomb["ov_flag"]) == (1, 1)
    outA = await await_out(dut, fresh=False)
    assert (outA["t_ns"] - t0) // CLK_NS + 1 == LAT, "A disturbed by the drop"
    check_exact(outA, vA, iA, 2000)
    vC = [15000] * 2000
    iC = [6000] * 2000
    tC = await feed_window(dut, vC, iC, 2000, True)
    outC = await await_out(dut)
    assert (outC["t_ns"] - tC) // CLK_NS + 1 == LAT
    check_exact(outC, vC, iC, 2000)
    assert (outC["ov"], outC["ov_flag"]) == (1, 1), "flag/counter must stick"
    cocotb.log.info("overrun: drop tombstoned, A intact, C exact, flag sticky")


@cocotb.test()
async def test_fsm_shared_sample_E(dut):
    """[S,E) ownership at stress and real rates: E excluded from old only."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    for cps in (1, REAL_CPS):
        await reset_dut(dut)
        await settle()
        v1 = [1000 + k for k in range(64)]
        i1 = [500 - k for k in range(64)]
        e_v, e_i = 32767, 32767  # distinctive full-scale E sample
        v2 = [2000 + k for k in range(64)]
        i2 = [100 + k for k in range(64)]
        # Window 1 = v1/i1 (E owned by window 2); window 2 = [E] + v2/i2.
        t1 = await feed_window(dut, v1, i1, 64, True, cps=cps, e_v=e_v, e_i=e_i)
        out1 = await await_out(dut)  # drain before window 2 (else overrun)
        assert (out1["t_ns"] - t1) // CLK_NS + 1 == LAT, f"cps={cps}"
        check_exact(out1, v1, i1, 64)
        # Window 2's samples start at E: drive E first WITH win_start.
        sv, vv, ii = dut.sample_valid, dut.v_q15, dut.i_q15
        ws, we = dut.win_start_pulse, dut.win_end_strobe
        gap_ns = (cps - 1) * CLK_NS
        full_v2 = [e_v] + v2
        full_i2 = [e_i] + i2
        for k in range(len(full_v2)):
            sv.value = 1
            vv.value = mask16(full_v2[k])
            ii.value = mask16(full_i2[k])
            ws.value = 1 if k == 0 else 0
            we.value = 0
            dut.M.value = 0
            dut.win_valid_in.value = 0
            await RisingEdge(dut.clk)
            await settle()
            sv.value = 0
            ws.value = 0
            if gap_ns:
                await Timer(gap_ns, unit="ns")
        sv.value = 1
        vv.value = mask16(1111)
        ii.value = mask16(2222)
        we.value = 1
        dut.M.value = 65
        dut.win_valid_in.value = 1
        await RisingEdge(dut.clk)
        t2 = edge_now_ns()
        sv.value = 0
        we.value = 0
        out2 = await await_out(dut)
        assert (out2["t_ns"] - t2) // CLK_NS + 1 == LAT, f"cps={cps}"
        check_exact(out2, full_v2, full_i2, 65)
    cocotb.log.info("shared-E: ownership exact at 1 and 1200 clocks/sample")


@cocotb.test()
async def test_fsm_fullscale_E_drop(dut):
    """Drop coinciding with a full-scale E: E goes nowhere but the next window.

    B's win_end lands mid-finalization with E_B full-scale; B is tombstoned
    (its partial sums discarded); C accumulates from E_B and must match the
    golden INCLUDING E_B — proving E was neither double-counted nor lost.
    """
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    vA = [9000] * 2000
    iA = [4000] * 2000
    t0 = await feed_window(dut, vA, iA, 2000, True)
    vB = [300] * 200
    iB = [150] * 200
    tB = await feed_window(dut, vB, iB, 200, True, e_v=32767, e_i=32767)
    assert (tB - t0) // CLK_NS < LAT, "B must end while A finalizes"
    tomb = await await_out(dut)
    assert tomb["valid"] == 0 and (tomb["ov"], tomb["ov_flag"]) == (1, 1)
    outA = await await_out(dut, fresh=False)
    assert (outA["t_ns"] - t0) // CLK_NS + 1 == LAT
    check_exact(outA, vA, iA, 2000)
    # C starts at E_B (full-scale) WITH win_start, then 1999 more samples.
    vC = [4000 + (k % 7) for k in range(1999)]
    iC = [1000 - (k % 5) for k in range(1999)]
    fullC_v = [32767] + vC
    fullC_i = [32767] + iC
    sv, vv, ii = dut.sample_valid, dut.v_q15, dut.i_q15
    ws, we = dut.win_start_pulse, dut.win_end_strobe
    for k in range(len(fullC_v)):
        sv.value = 1
        vv.value = mask16(fullC_v[k])
        ii.value = mask16(fullC_i[k])
        ws.value = 1 if k == 0 else 0
        we.value = 0
        dut.M.value = 0
        dut.win_valid_in.value = 0
        await RisingEdge(dut.clk)
        await settle()
        sv.value = 0
        ws.value = 0
    sv.value = 1
    vv.value = mask16(5)
    ii.value = mask16(6)
    we.value = 1
    dut.M.value = 2000
    dut.win_valid_in.value = 1
    await RisingEdge(dut.clk)
    tC = edge_now_ns()
    sv.value = 0
    we.value = 0
    outC = await await_out(dut, fresh=False)
    assert (outC["t_ns"] - tC) // CLK_NS + 1 == LAT
    check_exact(outC, fullC_v, fullC_i, 2000)
    cocotb.log.info("fullscale-E-drop: E attributed once, C byte-exact")


@cocotb.test()
async def test_fsm_timing_edges(dut):
    """win_end before/on/after out_valid: drop / latch-new / normal."""
    start_clock(dut)
    # BEFORE: B ends 399 cycles after A's win_end (t reads 398 -> drop).
    await reset_dut(dut)
    await settle()
    vA = [7000] * 2000
    iA = [3000] * 2000
    t0 = await feed_window(dut, vA, iA, 2000, True)
    vB = [700] * 398
    iB = [350] * 398
    tB = await feed_window(dut, vB, iB, 398, True)
    assert (tB - t0) // CLK_NS == 398 + 1
    outA = await await_out(dut)  # A completes first ...
    assert (outA["t_ns"] - t0) // CLK_NS + 1 == LAT
    check_exact(outA, vA, iA, 2000)
    tomb = await await_out(dut, fresh=False)  # ... then the deferred tombstone
    assert tomb["valid"] == 0 and (tomb["ov"], tomb["ov_flag"]) == (1, 1)
    # ON: B ends exactly on A's completion edge (t reads 399 -> start).
    await reset_dut(dut)
    await settle()
    t0 = await feed_window(dut, vA, iA, 2000, True)
    vB = [700] * 399
    iB = [350] * 399
    tB = await feed_window(dut, vB, iB, 399, True)
    assert (tB - t0) // CLK_NS == 399 + 1
    outA = await await_out(dut)
    assert (outA["t_ns"] - t0) // CLK_NS + 1 == LAT
    check_exact(outA, vA, iA, 2000)
    assert (outA["ov"], outA["ov_flag"]) == (0, 0), "coincident edge must not drop"
    outB = await await_out(dut, fresh=False)
    assert (outB["t_ns"] - tB) // CLK_NS + 1 == LAT
    check_exact(outB, vB, iB, 399)
    # AFTER: B ends one cycle after completion (idle -> normal).
    await reset_dut(dut)
    await settle()
    t0 = await feed_window(dut, vA, iA, 2000, True)
    outA = await await_out(dut)
    assert (outA["t_ns"] - t0) // CLK_NS + 1 == LAT
    vB = [700] * 400
    iB = [350] * 400
    tB = await feed_window(dut, vB, iB, 400, True)
    outB = await await_out(dut)
    assert (outB["t_ns"] - tB) // CLK_NS + 1 == LAT
    check_exact(outB, vB, iB, 400)
    assert (outB["ov"], outB["ov_flag"]) == (0, 0)
    cocotb.log.info("timing-edges: before=drop, on=start, after=normal")


@cocotb.test()
async def test_fsm_overrun_saturate(dut):
    """Overrun counter saturation via test-only INIT (plus stickiness).

    Runs with parameters={"OVERRUN_CNT_INIT": 65534}: reset lands at FFFE
    (proving the override took effect); three rapid drops reach FFFF and
    stick (a wrap would read 0x0001); the in-flight window still completes
    exact; flag sticky throughout. Production and Yosys use the default.
    """
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    assert read_sig(dut.finalize_overrun_cnt, 16) == 0xFFFE
    assert read_sig(dut.finalize_overrun, 1) == 0
    vA = [11000] * 2000
    iA = [4500] * 2000
    t0 = await feed_window(dut, vA, iA, 2000, True)
    for m in (100, 100, 100):
        vB = [700] * m
        iB = [350] * m
        await feed_window(dut, vB, iB, m, True)
        tomb = await await_out(dut)
        assert tomb["valid"] == 0, "expected tombstone per drop"
    assert read_sig(dut.finalize_overrun_cnt, 16) == 0xFFFF
    assert read_sig(dut.finalize_overrun, 1) == 1
    outA = await await_out(dut, fresh=False)
    assert (outA["t_ns"] - t0) // CLK_NS + 1 == LAT
    check_exact(outA, vA, iA, 2000)
    cocotb.log.info("overrun-saturate: FFFE + 3 drops stick at FFFF")


@cocotb.test()
async def test_fsm_min_spacing(dut):
    """Clean windows at minimum valid spacing (M=1810): never busy, exact."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    for w in range(3):
        v = [12000 + ((w * 131 + k) % 500) for k in range(1810)]
        i = [6000 - ((w * 17 + k) % 300) for k in range(1810)]
        t_we = await feed_window(dut, v, i, 1810, True)
        out = await await_out(dut)
        assert (out["t_ns"] - t_we) // CLK_NS + 1 == LAT, f"window {w}"
        check_exact(out, v, i, 1810)
        assert (out["ov"], out["ov_flag"]) == (0, 0)
    cocotb.log.info("min-spacing: 3x1810-sample windows exact, no drops")
