"""Week 4b2 cocotb: record_agg vs tb/golden.py record_fields (toplevel=record_agg).

Proves the RTL record-level NEG_ENERGY clamp: signed energy increments
accumulate over valid sub-windows only, energy_prev_in adds once, clamp
once (total < 0 -> 0 + flags bit 5). P_AVG inputs (rec_p_sum/rec_m_total)
stay signed-exact; invalid slots record M 0 and contribute nothing.
Expected values come from record_fields on real (v, i) sub-windows, with
per-window (energy, p_sum, M) taken from window_power — the same
decomposition the power suite proves RTL-exact.
"""

import cocotb
import numpy as np
from cocotb.triggers import RisingEdge

from edge.attestation import GENESIS_HASH32, SubWindow, build_record
from tb.golden import record_fields, window_power
from tb.w4b_common import read_sig, reset_dut, settle, start_clock

NEG_BIT = 5


async def drive_slots(dut, slots, energy_prev, overseq=None):
    """Drive slot tuples (energy, p_sum, m, ok) + overrun snapshots.

    overseq[k] is the running w_overrun_cnt sampled with slot k (default
    all zero); return record outputs incl. rec_overrun_cnt.
    rec_done registers one cycle after the 5th w_done; outputs read then.
    """
    assert len(slots) == 5
    if overseq is None:
        overseq = [0] * 5
    assert len(overseq) == 5
    dut.energy_prev_in.value = energy_prev & ((1 << 64) - 1)
    for (energy, p_sum, m, ok), ov in zip(slots, overseq):
        dut.w_done.value = 1
        dut.w_energy.value = int(energy) & 0xFFFFFFFF
        dut.w_p_sum.value = int(p_sum) & ((1 << 64) - 1)
        dut.w_m.value = m
        dut.w_ok.value = 1 if ok else 0
        dut.w_overrun_cnt.value = ov
        await RisingEdge(dut.clk)
    # rec_done registers AT the 5th w_done edge and clears at the next, so
    # sample it in this cycle: drop w_done first (else edge6 opens a slot).
    dut.w_done.value = 0
    await settle()
    assert int(dut.rec_done.value) == 1, "rec_done never pulsed"
    out = {
        "energy": read_sig(dut.rec_energy, 64),
        "p_sum": read_sig(dut.rec_p_sum, 64, signed=True),
        "m_total": read_sig(dut.rec_m_total, 14),
        "zc": read_sig(dut.rec_zc, 60),
        "flags": read_sig(dut.rec_flags, 8),
        "overrun": read_sig(dut.rec_overrun_cnt, 8),
    }
    await RisingEdge(dut.clk)
    await settle()
    assert int(dut.rec_done.value) == 0, "rec_done not a 1-cycle pulse"
    return out


def slots_from_subs(subs):
    """Per-window (energy, p_sum, M, ok) from golden window_power."""
    slots = []
    for v, i, ok in subs:
        if not ok:
            slots.append((0, 0, 0, False))
            continue
        g = window_power([int(x) for x in v], [int(x) for x in i])
        slots.append((g.energy_uwh_inc, g.p_sum_q30, g.m, True))
    return slots


def check_record(out, subs, energy_prev, overrun_cnt=0, overseq=None):
    """Match RTL record outputs against record_fields + build_record."""
    got = record_fields(
        [([int(x) for x in v], [int(x) for x in i], ok) for v, i, ok in subs],
        energy_prev,
        overrun_cnt,
    )
    assert out["energy"] == got.energy_uwh, f"energy {out['energy']} != {got.energy_uwh}"
    assert out["flags"] == got.window_flags, f"flags {out['flags']:08b} != {got.window_flags:08b}"
    assert out["overrun"] == got.overrun_cnt, f"overrun {out['overrun']} != {got.overrun_cnt}"
    zc = [(out["zc"] >> (12 * k)) & 0xFFF for k in range(5)]
    assert zc == list(got.zc_samples), f"zc {zc} != {list(got.zc_samples)}"
    exp_p = sum(sum(a * b for a, b in zip(v, i)) for v, i, ok in subs if ok)
    assert out["p_sum"] == exp_p
    assert out["m_total"] == sum(len(v) for v, _, ok in subs if ok)
    record, _ = build_record(
        counter=3,
        window_start=30000,
        sub_windows=[
            SubWindow(
                v_q15=np.array(v, dtype=np.int64), i_q15=np.array(i, dtype=np.int64), valid=ok
            )
            for v, i, ok in subs
        ],
        device_id=1,
        prev_hash=GENESIS_HASH32,
        energy_prev_uwh=energy_prev,
        hmac_key=bytes(32),
        overrun_cnt=overrun_cnt,
    )
    assert got.energy_uwh == record["energy_uwh"]
    assert got.window_flags == record["window_flags"]
    assert got.overrun_cnt == record["overrun_cnt"] == overrun_cnt
    return got


def dc(m, v, i):
    return ([v] * m, [i] * m, True)


@cocotb.test()
async def test_record_mixed_sign_negative_total(dut):
    """(+ Lost,+,-,-,+) with negative total: clamp 0 + bit 5, exact P_AVG."""
    start_clock(dut)
    await reset_dut(dut)
    m = 2000
    subs = [
        dc(m, 20000, 15000),
        dc(m, 20000, 15000),
        dc(m, 20000, -20000),
        dc(m, 20000, -20000),
        dc(m, 5000, 5000),
    ]
    got = record_fields([([int(x) for x in v], [int(x) for x in i], ok) for v, i, ok in subs], 0)
    assert got.energy_uwh == 0 and (got.window_flags >> NEG_BIT) & 1 == 1
    out = await drive_slots(dut, slots_from_subs(subs), 0)
    check_record(out, subs, 0)
    cocotb.log.info("mixed-sign negative total: RTL clamps exactly like golden")


@cocotb.test()
async def test_record_mixed_sign_positive_total(dut):
    """(+,-,-,+,+) with positive total: passthrough, no flag."""
    start_clock(dut)
    await reset_dut(dut)
    m = 2000
    subs = [
        dc(m, 25000, 20000),
        dc(m, 20000, -20000),
        dc(m, 15000, -15000),
        dc(m, 25000, 20000),
        dc(m, 25000, 20000),
    ]
    got = record_fields([([int(x) for x in v], [int(x) for x in i], ok) for v, i, ok in subs], 0)
    assert got.energy_uwh > 0 and (got.window_flags >> NEG_BIT) & 1 == 0
    out = await drive_slots(dut, slots_from_subs(subs), 0)
    check_record(out, subs, 0)
    # Same mix on top of a nonzero cumulative energy: prev adds once.
    out2 = await drive_slots(dut, slots_from_subs(subs), 123456789)
    check_record(out2, subs, 123456789)
    assert out2["energy"] == 123456789 + got.energy_uwh
    cocotb.log.info("mixed-sign positive total: passthrough exact, prev adds once")


@cocotb.test()
async def test_record_zero_total(dut):
    """Exact-zero total passes through WITHOUT the flag (strict < 0)."""
    start_clock(dut)
    await reset_dut(dut)
    out = await drive_slots(
        dut,
        [
            (2000, 10**6, 2000, True),
            (-2000, -(10**6), 2000, True),
            (500, 10**5, 2000, True),
            (-500, -(10**5), 2000, True),
            (0, 0, 2000, True),
        ],
        0,
    )
    assert out["energy"] == 0
    assert out["flags"] == 0b11111, f"flags {out['flags']:08b}"
    assert out["p_sum"] == 0 and out["m_total"] == 5 * 2000
    cocotb.log.info("zero total: energy 0, bit 5 clear")


@cocotb.test()
async def test_record_all_invalid(dut):
    """All-invalid record: energy == prev, flags 0, sums 0."""
    start_clock(dut)
    await reset_dut(dut)
    out = await drive_slots(dut, [(999, 10**9, 2000, False)] * 5, 777)
    assert out["energy"] == 777
    assert out["flags"] == 0
    assert out["p_sum"] == 0 and out["m_total"] == 0
    zc = [(out["zc"] >> (12 * k)) & 0xFFF for k in range(5)]
    assert zc == [0] * 5
    cocotb.log.info("all-invalid: passthrough prev, zero sums")


@cocotb.test()
async def test_record_back_to_back_and_reset(dut):
    """Two consecutive records are independent; reset drops a partial record."""
    start_clock(dut)
    await reset_dut(dut)
    m = 500
    subs_a = [dc(m, 20000, 15000)] * 5
    subs_b = [dc(m, 20000, -20000)] * 5
    out_a = await drive_slots(dut, slots_from_subs(subs_a), 0)
    check_record(out_a, subs_a, 0)
    out_b = await drive_slots(dut, slots_from_subs(subs_b), out_a["energy"])
    check_record(out_b, subs_b, out_a["energy"])  # chained cumulative
    # Partial record then reset: only post-reset slots count.
    dut.w_done.value = 1
    dut.w_energy.value = 10**6 & 0xFFFFFFFF
    dut.w_p_sum.value = 10**9
    dut.w_m.value = 500
    dut.w_ok.value = 1
    dut.w_overrun_cnt.value = 0
    dut.energy_prev_in.value = 0
    await RisingEdge(dut.clk)
    dut.w_done.value = 0  # deassert before reset: no stray slot post-release
    await RisingEdge(dut.clk)
    await reset_dut(dut)
    out_c = await drive_slots(dut, slots_from_subs(subs_a), 0)
    check_record(out_c, subs_a, 0)
    cocotb.log.info("back-to-back + reset-mid-record: independent records")


@cocotb.test()
async def test_record_overrun_tombstone_slots(dut):
    """Week 5b: two tombstone slots ride as invalid/M0 with count + bit 6.

    Slots 1 and 3 are drops (zeros, w_ok=0) with running overrun snapshots
    [0,1,1,2,2]: the record shows invalid slots, rec_overrun_cnt == 2,
    flags bit 6 set — exactly record_fields/build_record with
    overrun_cnt=2.
    """
    start_clock(dut)
    await reset_dut(dut)
    m = 2000
    subs = [
        dc(m, 20000, 15000),
        ([0] * m, [0] * m, False),
        dc(m, 20000, 15000),
        ([0] * m, [0] * m, False),
        dc(m, 20000, 15000),
    ]
    out = await drive_slots(dut, slots_from_subs(subs), 0, overseq=[0, 1, 1, 2, 2])
    check_record(out, subs, 0, overrun_cnt=2)
    assert out["overrun"] == 2
    assert (out["flags"] >> 6) & 1 == 1, f"bit 6 clear: {out['flags']:08b}"
    zc = [(out["zc"] >> (12 * k)) & 0xFFF for k in range(5)]
    assert zc == [m, 0, m, 0, m], f"dropped slots must read M 0: {zc}"
    cocotb.log.info("overrun tombstones: invalid slots, count 2, bit 6")
