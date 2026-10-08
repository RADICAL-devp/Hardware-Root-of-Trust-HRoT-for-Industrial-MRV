"""Week 5b cocotb: power_calc -> record_agg chain (toplevel=power_record_chain).

Proves record-level overrun carriage end to end: six windows with window
3 dropped mid-record (tombstone) yield a record whose slot 2 is invalid
with M 0, rec_overrun_cnt == 1, flags bit 6 set — byte-exact vs
record_fields/build_record with overrun_cnt=1. A second clean record
(after the record-close cnt_clear) shows 0/bit-6-clear. Window sample
spans are TB-known, so golden sub-windows are exact.
"""

import cocotb
from cocotb.triggers import Event, RisingEdge

from tb.golden import record_fields, window_power
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


@cocotb.test()
async def test_p2r_clear_atomic(dut):
    """Clear on the record-close edge is atomic; drops after count fresh.

    Record 1 = [w1, w2, tomb(w3), w4, w5] (w3 drops mid-record, count 1).
    cnt_clear covers the close edge W+400 (W = w5 win_end); w6 (M=8) ends
    exactly ON it (coincident start); w7 drops on the very next cycle;
    w8..w10 close record 2. Asserts: rec1 carries count 1 intact despite
    the on-close clear; power counter reads 0 right after; w6 exact;
    w7 tombstone with a fresh count of 1; flag sticky; record 2 carries
    (invalid slot, M 0, count 1, bit 6) byte-exact vs golden.
    """
    start_clock(dut)
    dut.cnt_clear.value = 0
    dut.energy_prev_in.value = 0
    await reset_dut(dut)
    await settle()
    recs: list = []
    outs: list = []
    stop_ev = Event()

    async def monitor():
        while not stop_ev.is_set():
            await RisingEdge(dut.clk)
            await settle()
            if int(dut.rec_done.value):
                recs.append(
                    {
                        "energy": read_sig(dut.rec_energy, 64),
                        "flags": read_sig(dut.rec_flags, 8),
                        "zc": read_sig(dut.rec_zc, 60),
                        "overrun": read_sig(dut.rec_overrun_cnt, 8),
                    }
                )
            if int(dut.out_valid.value):
                outs.append(
                    {
                        "p_avg": read_sig(dut.p_avg_exact, 32, signed=True),
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
                )

    mon = cocotb.start_soon(monitor())
    edge = 0

    async def step(sv, v, i, ws, we, m, valid):
        nonlocal edge
        dut.sample_valid.value = sv
        dut.v_q15.value = v & 0xFFFF
        dut.i_q15.value = i & 0xFFFF
        dut.win_start_pulse.value = ws
        dut.win_end_strobe.value = we
        dut.M.value = m
        dut.win_valid_in.value = valid
        await RisingEdge(dut.clk)
        edge += 1
        await settle()

    async def feed_codes(vs, isi, m, valid):
        for k, (vv, ii) in enumerate(zip(vs, isi)):
            await step(1, vv, ii, 1 if k == 0 else 0, 0, 0, 0)
        await step(1, 7, 7, 0, 1, m, 1 if valid else 0)

    # Record 1: w1, w2 (M=2000), w3 short (M=200, drops mid-record),
    # w4, w5 (M=2000). Arrival order (tomb jumps queue): [w1, tomb, w2, w4, w5].
    spans = []
    for idx, m in enumerate((2000, 2000, 200, 2000, 2000)):
        v = [8000 + ((idx * 131 + j) % 500) for j in range(m)]
        i = [3000 - ((idx * 17 + j) % 300) for j in range(m)]
        spans.append((v, i))
        await feed_codes(v, i, m, True)
    # Idle to edge W+391 (W = w5 win_end edge); w6's samples then run to
    # the close edge W+400. cnt_clear goes high post-settle of W+399 so it
    # is sampled ONLY at the close edge: record_agg/TB sample the
    # pre-edge counter (w3's drop intact), then it zeroes. Any earlier
    # would wipe the still-needed running count (caught during bringup).
    for _ in range(391):
        await step(0, 0, 0, 0, 0, 0, 0)
    # w6 (M=8): samples then E exactly on the close edge W+400.
    v6 = [9000 + (j % 50) for j in range(8)]
    i6 = [4000 - (j % 30) for j in range(8)]
    for k in range(8):
        await step(1, v6[k], i6[k], 1 if k == 0 else 0, 0, 0, 0)
    dut.cnt_clear.value = 1
    await step(1, 11, 12, 0, 1, 8, 1)  # E + win_end on the close edge
    await settle()
    dut.cnt_clear.value = 0
    await settle()
    assert read_sig(dut.finalize_overrun_cnt, 16) == 0, "clear must zero the count"
    # w7: single E-sample with win_end on the cycle after close -> DROP.
    await step(1, 13, 14, 0, 1, 0, 1)
    # Chain record 2's cumulative energy: energy_prev_in is sampled at
    # close, so drive the precomputed record-1 energy BEFORE w8..w10 run.
    # (Arrival order [w1, tomb, w2, w4, w5]; validated against rec1 below.)
    win_all = [([int(x) for x in v], [int(x) for x in i], True) for v, i in spans]
    subs = [win_all[0], ([0] * 8, [0] * 8, False), win_all[1], win_all[3], win_all[4]]
    got1 = record_fields(subs, 0, overrun_cnt=1)
    dut.energy_prev_in.value = got1.energy_uwh & ((1 << 64) - 1)
    # w8..w10 clean M=2000 close record 2 (carries w7's drop).
    spans2 = [(v6, i6)]
    for idx in range(3):
        v = [9500 + ((idx * 131 + j) % 500) for j in range(2000)]
        i = [3600 - ((idx * 17 + j) % 300) for j in range(2000)]
        spans2.append((v, i))
        await feed_codes(v, i, 2000, True)
    for _ in range(450):
        await step(0, 0, 0, 0, 0, 0, 0)
    stop_ev.set()
    await mon

    # Record 1: drop carried (count 1, bit 6, tombstone slot) intact
    # despite the on-close clear.
    assert len(recs) >= 1
    rec1 = recs[0]
    assert rec1["overrun"] == 1, f"rec overrun {rec1['overrun']}"
    assert (rec1["flags"] >> 6) & 1 == 1, f"bit 6 clear: {rec1['flags']:08b}"
    assert [(rec1["zc"] >> (12 * s)) & 0xFFF for s in range(5)] == [
        2000,
        0,
        2000,
        2000,
        2000,
    ], "tombstone arrival-order slot must read M 0"
    # subs/got1 precomputed before the w8 feed (drives energy chaining too).
    assert rec1["energy"] == got1.energy_uwh
    assert rec1["flags"] == got1.window_flags
    exp_psum = 0
    for v, i, ok in subs:
        if ok:
            exp_psum += sum(a * b for a, b in zip(v, i))
    # w6 started on the close edge (not dropped): exact data, M 8. Its
    # snapshot already includes w7's later drop (running total semantics).
    w6outs = [o for o in outs if o["valid"] == 1 and o["m"] == 8]
    assert len(w6outs) == 1, "w6 must complete exactly once"
    g6 = window_power(v6, i6)
    o6 = w6outs[0]
    assert (o6["p_avg"], o6["vrms"], o6["irms"], o6["pf"], o6["energy"]) == (
        g6.p_avg_q30,
        g6.vrms_q15,
        g6.irms_q15,
        g6.pf_q15,
        g6.energy_uwh_inc,
    )
    assert o6["ov"] == 1, "running snapshot includes the later drop"
    # Tombstones: w3's (record 1) then w7's (record 2), each ov==1 flag==1.
    tombs = [o for o in outs if o["valid"] == 0]
    assert len(tombs) == 2, f"expected w3+w7 tombstones, got {len(tombs)}"
    for t in tombs:
        assert t["ov"] == 1 and t["ov_flag"] == 1
    assert read_sig(dut.finalize_overrun_cnt, 16) == 1
    assert read_sig(dut.finalize_overrun, 1) == 1
    # Record 2 carries w7's drop: invalid slot, M 0, count 1, bit 6.
    # Arrival order (tomb jumps queue): [w7tomb, w6, w8, w9, w10].
    assert len(recs) == 2, f"expected 2 records, got {len(recs)}"
    rec2 = recs[1]
    assert rec2["overrun"] == 1
    assert (rec2["flags"] >> 6) & 1 == 1
    assert [(rec2["zc"] >> (12 * s)) & 0xFFF for s in range(5)] == [0, 8, 2000, 2000, 2000]
    subs2 = [
        ([0] * 8, [0] * 8, False),
        ([int(x) for x in v6], [int(x) for x in i6], True),
        *[([int(x) for x in v], [int(x) for x in i], True) for v, i in spans2[1:]],
    ]
    got2 = record_fields(subs2, rec1["energy"], overrun_cnt=1)
    assert rec2["energy"] == got2.energy_uwh
    assert rec2["flags"] == got2.window_flags
    cocotb.log.info("clear-atomic: on-close clear intact, fresh drop counted, record 2 exact")
