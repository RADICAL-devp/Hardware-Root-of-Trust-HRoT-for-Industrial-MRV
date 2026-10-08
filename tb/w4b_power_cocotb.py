"""Week 4b cocotb: power_calc vs tb/golden.py window_power (toplevel=power_calc).

Register contract under test (see rtl/power_calc.v header):
- p_avg_exact == mean_q30_half_away(sum, M) EXACT (descriptor P_AVG target).
- p_avg_lut == lut_window_mean(sum, M) EXACT (divider-replacement path).
- vrms/irms == golden EXACT (incl. the 32768->32767 saturating corner).
- pf_q15 == golden EXACT. energy_uwh == energy_uwh_increment EXACT (signed;
  negative on negative-P windows — the 0+flag clamp is record-level).
- m_out == M (0 when invalid), p_sum_q30 == exact sum (record composition).
- Invalid (win_valid_in=0 or M=0): every data output 0, valid_out=0.
- out_valid pulses with EXACTLY 401 cycles inclusive latency after the
  win_end_strobe edge (Week 5b FSM schedule, DECISIONS.md derivation).
- The E-cycle sample (nonzero by construction) is EXCLUDED from the closed
  window — mis-steering by one sample fails loudly.
"""

import json
import os
from pathlib import Path

import cocotb
import numpy as np
from cocotb.triggers import RisingEdge

from edge.attestation import GENESIS_HASH32, SubWindow, build_record
from sensors.windows import energy_uwh_increment, lut_window_mean, mean_q30_half_away
from tb.golden import record_fields, window_power
from tb.w4b_common import (
    mask16,
    read_sig,
    reset_dut,
    settle,
    sine_codes,
    start_clock,
    synth_cross_stream,
)

REPO = Path(__file__).resolve().parents[1]
SEED = 42
M_MIN, M_MAX = 1810, 2230

_stats = {
    "vectors": 0,
    "latency": [],
    "err": {k: [] for k in ("p_avg_exact", "p_avg_lut", "vrms", "irms", "pf", "energy")},
    "lut_abs_lsb": [],
    "lut_rel": [],
}


async def drive_window(dut, v, i, m_port, valid, e_v=12345, e_i=-2345):
    """Drive len(v) samples + nonzero E sample; return (outputs, latency).

    m_port is the M value presented with win_end (normally len(v); 0 to
    prove the no-divide-by-zero invalid path). Latency counts clock cycles
    from the last INCLUDED sample's cycle (expected 401 per DECISIONS.md:
    the E-cycle sample closes the window but is excluded from it, so win_end is
    already 1 clock after the last sample).
    """
    n = len(v)
    assert len(i) == n
    sv, vv, ii = dut.sample_valid, dut.v_q15, dut.i_q15
    ws, we = dut.win_start_pulse, dut.win_end_strobe
    for k in range(n):
        sv.value = 1
        vv.value = mask16(v[k])
        ii.value = mask16(i[k])
        ws.value = 1 if k == 0 else 0
        we.value = 0
        dut.M.value = 0
        dut.win_valid_in.value = 0
        await RisingEdge(dut.clk)
    sv.value = 1
    vv.value = mask16(e_v)
    ii.value = mask16(e_i)
    ws.value = 0
    we.value = 1
    dut.M.value = m_port
    dut.win_valid_in.value = 1 if valid else 0
    await RisingEdge(dut.clk)
    sv.value = 0
    we.value = 0
    dut.M.value = 0
    lat = None
    for c in range(1, 450):
        await RisingEdge(dut.clk)
        await settle()
        if int(dut.out_valid.value):
            lat = c + 1  # +1: win_end presents E, one cycle after last sample
            break
    assert lat is not None, "out_valid never pulsed"
    out = {
        "p_avg_lut": read_sig(dut.p_avg_lut, 32, signed=True),
        "p_avg_exact": read_sig(dut.p_avg_exact, 32, signed=True),
        "vrms": read_sig(dut.vrms_q15, 16),
        "irms": read_sig(dut.irms_q15, 16),
        "pf": read_sig(dut.pf_q15, 16, signed=True),
        "energy": read_sig(dut.energy_uwh, 32, signed=True),
        "m": read_sig(dut.m_out, 12),
        "valid": int(dut.valid_out.value),
        "p_sum": read_sig(dut.p_sum_q30, 64, signed=True),
    }
    return out, lat


def check_against_golden(out, lat, v, i, m_port, valid):
    """Exact-match the RTL outputs against window_power (or zeros)."""
    assert lat == 401, f"cycle latency {lat}, expected 401"
    if not valid or m_port == 0:
        assert out["valid"] == 0, "invalid window reports valid"
        for k in ("p_avg_lut", "p_avg_exact", "vrms", "irms", "pf", "energy", "m", "p_sum"):
            assert out[k] == 0, f"invalid window leaks {k}={out[k]}"
        return
    assert len(v) == m_port, "unit windows carry M == sample count"
    g = window_power([int(x) for x in v], [int(x) for x in i])
    assert out["m"] == m_port == g.m
    assert out["p_sum"] == g.p_sum_q30, f"p_sum {out['p_sum']} != {g.p_sum_q30}"
    assert out["p_avg_exact"] == g.p_avg_q30, f"exact {out['p_avg_exact']} != {g.p_avg_q30}"
    # LUT path is defined only over the supported range (tiny directed
    # windows exercise the exact path; all 421 in-range M values are swept
    # exactly in test_lut_sweep_421).
    if M_MIN <= m_port <= M_MAX:
        assert out["p_avg_lut"] == lut_window_mean(g.p_sum_q30, m_port, M_MIN, 24)
    assert out["vrms"] == g.vrms_q15, f"vrms {out['vrms']} != {g.vrms_q15}"
    assert out["irms"] == g.irms_q15, f"irms {out['irms']} != {g.irms_q15}"
    assert out["pf"] == g.pf_q15, f"pf {out['pf']} != {g.pf_q15}"
    assert out["energy"] == g.energy_uwh_inc
    assert out["valid"] == 1


@cocotb.test()
async def test_power_directed(dut):
    """Directed power corners: full-scale, clamps, zeros, halves, headroom."""
    start_clock(dut)
    await reset_dut(dut)
    m = 2000
    big = 2230
    cases = [
        ([32767] * m, [32767] * m, m, True, "full-scale-pos"),
        ([32767] * m, [-32768] * m, m, True, "full-scale-neg"),
        ([-32768] * m, [32767] * m, m, True, "full-neg-dc-clamp"),
        ([0] * m, [0] * m, m, True, "all-zero"),
        ([2, 1], [1, 1], 2, True, "half-up-pos"),  # 3/2 -> 2
        ([-2, -1], [1, 1], 2, True, "half-away-neg"),  # -3/2 -> -2
        ([1, 1, 1, 1], [1, 1, 1, 1], 4, True, "tiny-exact"),  # 4/4 -> 1
        ([32767] * big, [32767] * big, big, True, "headroom-pos"),
        ([32767] * big, [-32768] * big, big, True, "headroom-neg"),
        ([20000] * m, [-20000] * m, m, True, "negative-P-window"),
        ([15000] * m, [6000] * m, m, False, "invalid-forced-zero"),
        ([15000] * 8, [6000] * 8, 0, True, "m-zero-forced-zero"),
        ([1, 0], [1, 0], 2, True, "tiny-energy-rounds-to-zero"),
    ]
    for v, i, m_port, valid, name in cases:
        n = m_port if m_port else 8
        assert len(v) == n
        out, lat = await drive_window(dut, v, i, m_port, valid)
        check_against_golden(out, lat, v, i, m_port, valid)
        cocotb.log.info(f"directed {name}: ok (lat={lat})")
    g = window_power([1, 0], [1, 0])  # p_sum = 1 -> increment rounds to 0
    assert g.energy_uwh_inc == energy_uwh_increment(1, 2) == 0


@cocotb.test()
async def test_lut_sweep_421(dut):
    """All 421 M values through the reciprocal LUT against the golden.

    Short 3-sample windows with the M PORT swept over [1810, 2230]: this
    isolates the mean/energy paths by design (power_calc trusts M; range
    validation is zc_detect's job). p_avg_lut exercises the ROM at every
    index 0..420; p_avg_exact and energy exercise the dividers at every M.
    (vrms/irms/PF need M == sample count and are covered everywhere else.)
    """
    start_clock(dut)
    await reset_dut(dut)
    rng = np.random.default_rng(SEED)
    for m in range(M_MIN, M_MAX + 1):
        v = [int(x) for x in rng.integers(-32768, 32768, size=3)]
        i = [int(x) for x in rng.integers(-32768, 32768, size=3)]
        out, lat = await drive_window(dut, v, i, m, True)
        s = sum(a * b for a, b in zip(v, i))
        assert lat == 401, f"M={m}: latency {lat}"
        assert out["m"] == m, f"M={m}: m_out {out['m']}"
        assert out["p_avg_lut"] == lut_window_mean(s, m, M_MIN, 24), f"M={m}: LUT path"
        assert out["p_avg_exact"] == mean_q30_half_away(s, m), f"M={m}: exact path"
        assert out["energy"] == energy_uwh_increment(s, m), f"M={m}: energy path"
    cocotb.log.info("LUT sweep: all 421 M values exact on lut/exact/energy paths")


@cocotb.test()
async def test_record_5x(dut):
    """Five back-to-back windows (same-cycle end+start) compose to record_fields.

    Covers the negative-P record: five anti-phase windows whose total energy
    is negative -> ENERGY_UWH 0 + flags bit 5 + signed-exact P_AVG, equal to
    record_fields AND edge/attestation.build_record byte-for-byte.
    """
    start_clock(dut)
    await reset_dut(dut)
    for tag, i_level in (("pos", 8000), ("neg", -20000)):
        gap, n_det = 200, 51
        det = [2 + k * gap for k in range(n_det)]
        length = det[-1] + 4
        v = synth_cross_stream(length, det)
        i = [i_level] * length
        starts = det[0:50:10]
        ends = det[10:51:10]
        assert len(starts) == len(ends) == 5
        assert [e - s for s, e in zip(starts, ends)] == [2000] * 5
        sv, vv, ii = dut.sample_valid, dut.v_q15, dut.i_q15
        ws, we = dut.win_start_pulse, dut.win_end_strobe
        sset, eset = set(starts), set(ends)
        e2s = {e: s for s, e in zip(starts, ends)}
        outs = []
        for k in range(length):
            sv.value = 1
            vv.value = v[k] & 0xFFFF
            ii.value = i[k] & 0xFFFF
            ws.value = 1 if k in sset else 0
            we.value = 1 if k in eset else 0
            if k in eset:
                dut.M.value = k - e2s[k]
                dut.win_valid_in.value = 1
            else:
                dut.M.value = 0
                dut.win_valid_in.value = 0
            await RisingEdge(dut.clk)
            await settle()
            if int(dut.out_valid.value):
                outs.append(
                    {
                        "p_sum": read_sig(dut.p_sum_q30, 64, signed=True),
                        "energy": read_sig(dut.energy_uwh, 32, signed=True),
                        "m": read_sig(dut.m_out, 12),
                        "valid": int(dut.valid_out.value),
                        "p_avg": read_sig(dut.p_avg_exact, 32, signed=True),
                    }
                )
        sv.value = 0
        ws.value = 0
        we.value = 0
        for _ in range(450):  # final window needs the full 401-cycle schedule
            await RisingEdge(dut.clk)
            await settle()
            if int(dut.out_valid.value):
                outs.append(
                    {
                        "p_sum": read_sig(dut.p_sum_q30, 64, signed=True),
                        "energy": read_sig(dut.energy_uwh, 32, signed=True),
                        "m": read_sig(dut.m_out, 12),
                        "valid": int(dut.valid_out.value),
                        "p_avg": read_sig(dut.p_avg_exact, 32, signed=True),
                    }
                )
        assert len(outs) == 5, f"{tag}: got {len(outs)} completions"
        subs = [(v[s:e], i[s:e], True) for s, e in zip(starts, ends)]
        got = record_fields([([int(x) for x in a], [int(x) for x in b], c) for a, b, c in subs], 0)
        assert [o["m"] for o in outs] == list(got.zc_samples) == [2000] * 5
        assert [o["valid"] for o in outs] == [1] * 5
        assert sum(o["p_sum"] for o in outs) == sum(
            sum(a * b for a, b in zip(aa, bb)) for aa, bb, _ in subs
        )
        rec_pavg = mean_q30_half_away(sum(o["p_sum"] for o in outs), 5 * 2000)
        assert rec_pavg == got.p_avg_q30
        if tag == "neg":
            for o in outs:
                assert o["energy"] < 0, "raw per-window increments stay signed"
            assert sum(o["energy"] for o in outs) < 0
            assert got.energy_uwh == 0 and (got.window_flags >> 5) & 1 == 1
        else:
            assert sum(o["energy"] for o in outs) == got.energy_uwh > 0
        record, _ = build_record(
            counter=0,
            window_start=starts[0],
            sub_windows=[
                SubWindow(
                    v_q15=np.array(a, dtype=np.int64), i_q15=np.array(b, dtype=np.int64), valid=c
                )
                for a, b, c in subs
            ],
            device_id=1,
            prev_hash=GENESIS_HASH32,
            energy_prev_uwh=0,
            hmac_key=bytes(32),
        )
        assert got.p_avg_q30 == record["p_avg_q30"]
        assert got.energy_uwh == record["energy_uwh"]
        assert list(got.zc_samples) == record["zc_samples"]
        assert got.window_flags == record["window_flags"]
        cocotb.log.info(f"record {tag}: fields match golden+attestation exactly")


@cocotb.test()
async def test_random_1000(dut):
    """1,000 seeded vectors, full-field exact match; stats to results JSON."""
    start_clock(dut)
    await reset_dut(dut)
    rng = np.random.default_rng(SEED)
    kinds = ["sine"] * 600 + ["uniform"] * 250 + ["dc"] * 150
    rng.shuffle(kinds)
    for t in range(1000):
        m = int(rng.integers(M_MIN, M_MAX + 1))
        kind = kinds[t]
        if kind == "sine":
            v, i = sine_codes(
                m,
                f_hz=float(rng.uniform(49.5, 50.5)),
                vpk=float(rng.uniform(20000, 30000)),
                ipk=float(rng.uniform(5000, 12000)),
                phi_rad=float(rng.uniform(-0.4, 0.4)),
                seed=int(rng.integers(0, 2**31)),
            )
        elif kind == "uniform":
            v = [int(x) for x in rng.integers(-32768, 32768, size=m)]
            i = [int(x) for x in rng.integers(-32768, 32768, size=m)]
        else:
            vv, ii = int(rng.integers(-30000, 30001)), int(rng.integers(-30000, 30001))
            v, i = [vv] * m, [ii] * m
        g = window_power(v, i)
        out, lat = await drive_window(dut, v, i, m, True)
        _stats["vectors"] += 1
        _stats["latency"].append(lat)
        assert lat == 401, f"vector {t}: latency {lat}"
        for key, good in (
            ("p_avg_exact", g.p_avg_q30),
            ("p_avg_lut", lut_window_mean(g.p_sum_q30, m, M_MIN, 24)),
            ("vrms", g.vrms_q15),
            ("irms", g.irms_q15),
            ("pf", g.pf_q15),
            ("energy", g.energy_uwh_inc),
        ):
            err = abs(out[key] - good)
            _stats["err"][key].append(err)
            assert err == 0, f"vector {t} ({kind} M={m}): {key} {out[key]} != {good}"
        assert out["m"] == m and out["valid"] == 1 and out["p_sum"] == g.p_sum_q30
        # LUT-vs-exact characterization on realistic sums (report only; both
        # paths are asserted exact above).
        _stats["lut_abs_lsb"].append(abs(out["p_avg_lut"] - out["p_avg_exact"]))
        if out["p_avg_exact"] != 0:
            _stats["lut_rel"].append(
                abs(out["p_avg_lut"] - out["p_avg_exact"]) / abs(out["p_avg_exact"])
            )
    tag = os.environ.get("WEEK4B_TAG", "unknown")
    lat = _stats["latency"]
    payload = {
        "sim": tag,
        "vectors": _stats["vectors"],
        "latency_cycles": {"min": min(lat), "max": max(lat)},
        "lsb_error": {
            k: {"max": max(v), "mean": sum(v) / len(v)} for k, v in _stats["err"].items()
        },
        "lut_vs_exact": {
            "worst_abs_lsb": max(_stats["lut_abs_lsb"]) if _stats["lut_abs_lsb"] else 0,
            "worst_rel": max(_stats["lut_rel"]) if _stats["lut_rel"] else 0.0,
        },
    }
    path = REPO / "results" / f"week4b_{tag}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    cocotb.log.info(f"random_1000 [{tag}]: all exact, wrote {path}")
