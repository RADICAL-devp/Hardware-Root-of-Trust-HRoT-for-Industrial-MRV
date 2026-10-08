"""Week 5b cocotb: power_calc -> record_agg chain (toplevel=power_record_chain).

Proves record-level overrun carriage end to end: six windows with window
3 dropped mid-record (tombstone) yield a record whose slot 2 is invalid
with M 0, rec_overrun_cnt == 1, flags bit 6 set — byte-exact vs
record_fields/build_record with overrun_cnt=1. A second clean record
(after the record-close cnt_clear) shows 0/bit-6-clear. Window sample
spans are TB-known, so golden sub-windows are exact.
"""

import cocotb
from cocotb.triggers import RisingEdge

from tb.golden import record_fields
from tb.w4b_common import read_sig, reset_dut, settle, start_clock
from tb.w5_powerfsm_cocotb import feed_window

LAT = 401  # [cycles] inclusive win_end -> out_valid (DECISIONS.md)


async def await_rec(dut, timeout=3000):
    """Wait for rec_done; return record outputs."""
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await settle()
        if int(dut.rec_done.value):
            return {
                "energy": read_sig(dut.rec_energy, 64),
                "p_sum": read_sig(dut.rec_p_sum, 64, signed=True),
                "m_total": read_sig(dut.rec_m_total, 14),
                "zc": read_sig(dut.rec_zc, 60),
                "flags": read_sig(dut.rec_flags, 8),
                "overrun": read_sig(dut.rec_overrun_cnt, 8),
            }
    raise AssertionError("rec_done never pulsed")


async def pulse_clear(dut):
    """Record-close protocol: consume outputs, then pulse cnt_clear."""
    dut.cnt_clear.value = 1
    await RisingEdge(dut.clk)
    await settle()
    dut.cnt_clear.value = 0
    await RisingEdge(dut.clk)
    await settle()


@cocotb.test()
async def test_p2r_overrun_record(dut):
    """Drop mid-record -> invalid slot/M0/count 1/bit 6; clean record after."""
    start_clock(dut)
    dut.cnt_clear.value = 0
    dut.energy_prev_in.value = 0
    await reset_dut(dut)
    await settle()
    # Six windows: w1, w2 normal (M=2000); w3 short (M=200, win_end lands
    # mid-finalization -> DROP); w4-w6 normal. Record 1 = slots w1..w5.
    ms = [2000, 2000, 200, 2000, 2000, 2000]
    spans = []
    for idx, m in enumerate(ms[:5]):
        v = [8000 + ((idx * 131 + j) % 500) for j in range(m)]
        i = [3000 - ((idx * 17 + j) % 300) for j in range(m)]
        spans.append((v, i))
        await feed_window(dut, v, i, m, True)
    # Record 1 closes at w5's out_valid: await BEFORE feeding w6 (else the
    # rec_done pulse is history by the time we poll).
    rec1 = await await_rec(dut)
    assert rec1["overrun"] == 1, f"rec overrun {rec1['overrun']}"
    assert (rec1["flags"] >> 6) & 1 == 1, f"bit 6 clear: {rec1['flags']:08b}"
    # Slots fill in ARRIVAL order, not window order: the tombstone (1-cycle
    # latency) lands before w2's data (401-cycle) — totals stay exact.
    zc = [(rec1["zc"] >> (12 * s)) & 0xFFF for s in range(5)]
    assert zc == [2000, 0, 2000, 2000, 2000], f"dropped slot must read M 0: {zc}"
    # Arrival order: [w1, tomb(w3), w2, w4, w5]; w3's data never arrives.
    win = [([int(x) for x in v], [int(x) for x in i], True) for v, i in spans[:5]]
    subs = [win[0], ([0] * 8, [0] * 8, False), win[1], win[3], win[4]]
    got = record_fields(subs, 0, overrun_cnt=1)
    assert rec1["energy"] == got.energy_uwh
    assert rec1["flags"] == got.window_flags
    exp_psum = 0
    for v, i, ok in subs:
        if ok:
            exp_psum += sum(a * b for a, b in zip(v, i))
    assert rec1["p_sum"] == exp_psum
    await pulse_clear(dut)
    assert read_sig(dut.finalize_overrun_cnt, 16) == 0, "clear must zero the counter"
    # Record 2 (w6 + 4 clean windows): overrun 0, bit 6 clear. Chain the
    # cumulative energy: prev is sampled at close, so set it any time now.
    dut.energy_prev_in.value = rec1["energy"] & ((1 << 64) - 1)
    m6 = ms[5]
    v6 = [8000 + ((5 * 131 + j) % 500) for j in range(m6)]
    i6 = [3000 - ((5 * 17 + j) % 300) for j in range(m6)]
    spans.append((v6, i6))
    await feed_window(dut, v6, i6, m6, True)
    for idx in range(4):
        m = 2000
        v = [9000 + ((idx * 131 + j) % 500) for j in range(m)]
        i = [3500 - ((idx * 17 + j) % 300) for j in range(m)]
        spans.append((v, i))
        await feed_window(dut, v, i, m, True)
    rec2 = await await_rec(dut)
    assert rec2["overrun"] == 0
    assert (rec2["flags"] >> 6) & 1 == 0
    subs2 = [([int(x) for x in v], [int(x) for x in i], True) for v, i in spans[5:10]]
    got2 = record_fields(subs2, rec1["energy"], overrun_cnt=0)
    assert rec2["energy"] == got2.energy_uwh
    assert rec2["flags"] == got2.window_flags
    cocotb.log.info("p2r: mid-record drop carried exact; clean record after clear")
