"""Week 4a: bit-exact golden power model vs float reference + rounding spec.

Covers tb/golden.py: UART parse cross-check vs edge.framing, 1,000 seeded
power vectors vs float64, exact-half/negative rounding, saturation
corners, integer-exact energy, descriptor-field equality with
edge/attestation.build_record, and accumulator-width bounds.
"""

from fractions import Fraction

import numpy as np
import pytest

from edge.attestation import GENESIS_HASH32, SubWindow, build_record
from edge.framing import FrameDecoder, encode_frame
from sensors.windows import (
    div_round_half_away,
    energy_uwh_increment,
    isqrt_round_half_up,
    mean_q30_half_away,
)
from tb.golden import (
    I16_MAX,
    P_FS_W,
    Q15_SCALE,
    Q30_SCALE,
    parse_l0_stream,
    record_fields,
    window_power,
)

SEED = 42
LSB_W = P_FS_W / Q30_SCALE  # [W] one Q30 power LSB
LSB_V = 500.0 / 32768.0  # [V] one Q15 voltage LSB


def test_uart_parse_matches_framing_exactly():
    rng = np.random.default_rng(SEED)
    frames = [
        encode_frame(k, int(rng.integers(-32768, 32768)), int(rng.integers(-32768, 32768)))
        for k in range(300)
    ]
    garbage = bytes(rng.integers(0, 256, size=97).astype(np.uint8))
    stream = garbage + b"".join(frames) + bytes(rng.integers(0, 256, size=5).astype(np.uint8))
    parsed = parse_l0_stream(stream)
    dec = FrameDecoder()
    ref = dec.feed(stream)
    assert [(s.counter, s.v_q15, s.i_q15) for s in parsed] == [
        (f.counter, f.v_q15, f.i_q15) for f in ref
    ]
    assert [s.counter for s in parsed] == list(range(300))


def test_power_vs_float_reference_1000_vectors():
    rng = np.random.default_rng(SEED)
    worst_p, worst_rms, worst_pf = 0.0, 0.0, 0.0
    for _ in range(1000):  # seeded random vectors, realistic M range
        m = int(rng.integers(1810, 2231))
        v = rng.integers(-32768, 32768, size=m).astype(float)
        i = rng.integers(-32768, 32768, size=m).astype(float)
        g = window_power([int(x) for x in v], [int(x) for x in i])
        p_ref = float(np.mean(v * i)) / Q15_SCALE**2 * P_FS_W  # [W]
        p_got = g.p_avg_q30 / Q30_SCALE * P_FS_W  # [W]
        worst_p = max(worst_p, abs(p_got - p_ref))
        vrms_ref = float(np.sqrt(np.mean(v * v))) / Q15_SCALE * 500.0
        worst_rms = max(worst_rms, abs(g.vrms_q15 / 32768.0 * 500.0 - vrms_ref))
        if vrms_ref > 0 and float(np.sqrt(np.mean(i * i))) > 0:
            pf_ref = p_ref / (vrms_ref * float(np.sqrt(np.mean(i * i))) / Q15_SCALE * 100.0)
            worst_pf = max(worst_pf, abs(g.pf_q15 / 32768.0 - pf_ref))
    assert worst_p <= 0.5 * LSB_W + 1e-9  # mean rounding only
    assert worst_rms <= 1.0 * LSB_V + 1e-9  # mean + sqrt rounding
    assert worst_pf <= 2.0 / 32768.0  # quotient rounding, bounded


def test_rounding_halves_and_negatives():
    assert div_round_half_away(7, 2) == 4  # 3.5 → 4
    assert div_round_half_away(-7, 2) == -4  # -3.5 → -4 (symmetric, not banker's)
    assert div_round_half_away(5, 2) == 3
    assert div_round_half_away(-5, 2) == -3
    assert div_round_half_away(6, 4) == 2  # 1.5 → 2
    assert div_round_half_away(-6, 4) == -2
    assert div_round_half_away(6, 3) == 2  # exact stays exact
    assert mean_q30_half_away(-7, 2) == -4
    with pytest.raises(ValueError):
        div_round_half_away(1, 0)
    with pytest.raises(ValueError):
        mean_q30_half_away(10, 0)
    # isqrt: rem > root rounds up (half-up); rem == root stays down.
    assert isqrt_round_half_up(0) == 0
    assert isqrt_round_half_up(2) == 1  # 1.414, rem 1 == root 1 → down
    assert isqrt_round_half_up(3) == 2  # 1.732, rem 2 > root 1 → up
    assert isqrt_round_half_up(6) == 2  # 2.449, rem 2 == root 2 → down
    assert isqrt_round_half_up(8) == 3  # 2.828, rem 4 > root 2 → up
    with pytest.raises(ValueError):
        isqrt_round_half_up(-1)


def test_saturation_corners():
    m = 2000
    g = window_power([-32768] * m, [32767] * m)  # full-negative DC voltage
    assert g.vrms_q15 == I16_MAX  # root 32768 saturates: the single corner, 1 LSB
    assert g.pf_q15 == -32768  # full negative power saturates PF
    z = window_power([0] * m, [0] * m)  # all-zero window: defined, no crash
    assert (z.p_avg_q30, z.vrms_q15, z.irms_q15, z.pf_q15, z.energy_uwh_inc) == (0, 0, 0, 0, 0)
    full = window_power([32767] * m, [32767] * m)  # full-positive DC: exact, no clip
    assert full.vrms_q15 == 32767 and full.irms_q15 == 32767


def test_energy_integer_exact_vs_fraction():
    rng = np.random.default_rng(SEED)
    for _ in range(200):  # incl. negative power sums (regenerative corner)
        p_sum = int(rng.integers(-(1 << 42), 1 << 42))
        m = int(rng.integers(1810, 2231))
        got = energy_uwh_increment(p_sum, m)
        f = Fraction(p_sum * 12500, 9 * (1 << 30))
        fl = f.numerator // f.denominator if f >= 0 else -((-f).numerator // (-f).denominator)
        frac = abs(f - fl)
        expected = (
            fl
            + (1 if f >= 0 and frac >= Fraction(1, 2) else 0)
            - (1 if f < 0 and frac >= Fraction(1, 2) else 0)
        )
        assert got == expected
    assert energy_uwh_increment(0, 2000) == 0


def test_old_vs_new_rounding_differs_only_on_halves():
    # Week 4a migration evidence: Python banker's round() (old, in
    # edge/attestation) vs canonical half-away (new) agree everywhere
    # except exact halves, and there by exactly 1 LSB. No Week 3 test
    # pinned absolute values (all accept/reject + counters), and 0/24
    # real record windows differed — so nothing was regenerated to pass.
    rng = np.random.default_rng(SEED)
    diffs = 0
    for _ in range(5000):
        s = int(rng.integers(-(1 << 42), 1 << 42))
        m = int(rng.integers(1, 2231))
        old = int(round(s / m))
        new = mean_q30_half_away(s, m)
        if old != new:
            diffs += 1
            assert abs(old - new) == 1  # never more than 1 LSB
            f = Fraction(s, m)
            assert f.denominator == 2  # exact half: the only divergent case
            assert old % 2 == 0  # banker's side rounds to even; half-away does not
    assert diffs > 0  # halves do occur: the migration is load-bearing in theory
    # Exact characterization on constructed halves (no placeholder logic).
    assert int(round(7 / 2)) == 4 and mean_q30_half_away(7, 2) == 4  # agree (odd .5 up)
    assert int(round(5 / 2)) == 2 and mean_q30_half_away(5, 2) == 3  # differ by 1


def test_record_fields_match_attestation_exactly():
    rng = np.random.default_rng(SEED)
    for trial in range(20):
        subs, att_subs = [], []
        for _ in range(5):
            valid = bool(rng.random() < 0.8)
            m = int(rng.integers(1900, 2100))
            v = [int(x) for x in rng.integers(-30000, 30000, size=m)]
            i = [int(x) for x in rng.integers(-30000, 30000, size=m)]
            subs.append((v, i, valid))
            att_subs.append(
                SubWindow(
                    v_q15=np.array(v, dtype=np.int64),
                    i_q15=np.array(i, dtype=np.int64),
                    valid=valid,
                )
            )
        energy_prev = int(rng.integers(0, 10**9))
        got = record_fields(subs, energy_prev)
        record, _ = build_record(
            counter=trial,
            window_start=trial * 10_000,
            sub_windows=att_subs,
            device_id=1,
            prev_hash=GENESIS_HASH32,
            energy_prev_uwh=energy_prev,
            hmac_key=bytes(32),
        )
        assert got.p_avg_q30 == record["p_avg_q30"]
        assert got.energy_uwh == record["energy_uwh"]
        assert list(got.zc_samples) == record["zc_samples"]
        assert got.window_flags == record["window_flags"]


def test_accumulator_widths_hold_at_maximum():
    m = 2230  # [samples] D-05 ceiling
    v = [32767] * m
    i = [-32768] * m
    g = window_power(v, i)
    assert abs(g.p_sum_q30) < (1 << 42)  # 43-bit signed minimum holds
    from tb.golden import ACCUM_BITS

    assert ACCUM_BITS == 43


def test_full_scale_all_five_valid_record_total():
    # Record totals span 5 sub-windows (11150 samples): the Week 4a
    # "43-bit" figure was per sub-window; the record needs 45-bit signed.
    from fractions import Fraction as Frac

    from tb.golden import RECORD_ACCUM_BITS

    m = 2230
    subs = [([32767] * m, [32767] * m, True) for _ in range(5)]
    got = record_fields(subs, 0)
    p_each = 32767 * 32767
    assert got.p_avg_q30 == p_each  # uniform full-scale: mean equals samples
    total = 5 * m * p_each
    assert total < (1 << 44) and total >= (1 << 43)  # 44 magnitude bits + sign
    assert RECORD_ACCUM_BITS == 45
    e_each = int(Frac(m * p_each * 12500, 9 * (1 << 30)) + Frac(1, 2))
    assert got.energy_uwh == 5 * e_each == 15_485_165
    assert got.energy_uwh * 10**12 < (1 << 64)  # u64 headroom > 10^12 records
    assert list(got.zc_samples) == [m] * 5 and got.window_flags == 0b11111
    # Opposite corner: full negative power on all five (magnitude bound).
    subs_neg = [([32767] * m, [-32768] * m, True) for _ in range(5)]
    got_neg = record_fields(subs_neg, 0)
    assert abs(5 * m * 32767 * -32768) < (1 << 44)
    assert got_neg.p_avg_q30 == -32767 * 32768  # signed-exact, no wrap
