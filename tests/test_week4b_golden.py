"""Week 4b: NEG_ENERGY clamp in tb/golden.py record_fields.

Pins that record_fields mirrors edge/attestation.build_record exactly on
negative-total records (clamp ENERGY_UWH to 0 + flags bit 5, P_AVG
signed-exact), so the RTL record-composition check has a single truth.
Per-window sums stay UNCLAMPED (raw signed increments); only the record
total clamps — mixed-sign records with a non-negative total pass through
unchanged, identical to build_record.
"""

import numpy as np

from edge.attestation import GENESIS_HASH32, NEG_ENERGY_BIT, SubWindow, build_record
from tb.golden import record_fields

SEED = 42


def _att_subs(subs):
    return [
        SubWindow(
            v_q15=np.array(v, dtype=np.int64),
            i_q15=np.array(i, dtype=np.int64),
            valid=valid,
        )
        for (v, i, valid) in subs
    ]


def test_neg_energy_clamp_matches_attestation():
    # Five all-negative DC windows: record total must clamp to 0 + bit 5.
    m = 2000
    subs = [([20000] * m, [-20000] * m, True) for _ in range(5)]
    got = record_fields(subs, 0)
    record, _ = build_record(
        counter=7,
        window_start=70_000,
        sub_windows=_att_subs(subs),
        device_id=1,
        prev_hash=GENESIS_HASH32,
        energy_prev_uwh=0,
        hmac_key=bytes(32),
    )
    assert got.energy_uwh == 0
    assert got.window_flags == record["window_flags"] == 0b11111 | (1 << NEG_ENERGY_BIT)
    assert got.neg_energy_clamped is True
    assert record["neg_energy_clamped"] is True
    # P_AVG stays signed-exact: uniform DC, mean equals the sample product.
    assert got.p_avg_q30 == record["p_avg_q30"] == -(20000 * 20000)
    assert list(got.zc_samples) == record["zc_samples"] == [m] * 5


def test_positive_total_never_clamps():
    rng = np.random.default_rng(SEED)
    for trial in range(10):
        subs = []
        for _ in range(5):
            valid = bool(rng.random() < 0.8)
            m = int(rng.integers(1900, 2100))
            v = [int(x) for x in rng.integers(20000, 30000, size=m)]
            i = [int(x) for x in rng.integers(5000, 15000, size=m)]
            subs.append((v, i, valid))
        energy_prev = int(rng.integers(0, 10**6))
        got = record_fields(subs, energy_prev)
        record, _ = build_record(
            counter=trial,
            window_start=trial * 10_000,
            sub_windows=_att_subs(subs),
            device_id=1,
            prev_hash=GENESIS_HASH32,
            energy_prev_uwh=energy_prev,
            hmac_key=bytes(32),
        )
        assert (got.window_flags >> NEG_ENERGY_BIT) & 1 == 0
        assert got.neg_energy_clamped is False
        assert got.energy_uwh == record["energy_uwh"] > 0
        assert got.p_avg_q30 == record["p_avg_q30"]
        assert got.window_flags == record["window_flags"]


def test_mixed_sign_nonnegative_total_passes_through():
    # One negative window + four positive: per-window sums stay unclamped,
    # total stays non-negative, both models agree exactly.
    m = 2000
    neg = ([10000] * m, [-30000] * m, True)
    pos = ([25000] * m, [20000] * m, True)
    subs = [neg] + [pos] * 4
    got = record_fields(subs, 0)
    record, _ = build_record(
        counter=0,
        window_start=0,
        sub_windows=_att_subs(subs),
        device_id=1,
        prev_hash=GENESIS_HASH32,
        energy_prev_uwh=0,
        hmac_key=bytes(32),
    )
    assert got.energy_uwh == record["energy_uwh"] > 0
    assert got.p_avg_q30 == record["p_avg_q30"]
    assert list(got.zc_samples) == [m] * 5
    assert got.window_flags == record["window_flags"] == 0b11111


def test_all_invalid_record_is_zero():
    subs = [([15000] * 200, [6000] * 200, False) for _ in range(5)]
    got = record_fields(subs, 0)
    assert (got.p_avg_q30, got.energy_uwh) == (0, 0)
    assert list(got.zc_samples) == [0] * 5
    assert got.window_flags == 0
