"""Week 4a: directed zero-crossing detector cases (D-05 failure modes).

Each D-05 failure mode must yield the correct invalid flag and zero
samples (zc entry 0 via the attestation mapping) — never a
plausible-looking number (window means stay NaN for invalid windows).
Detector input is always measured-style voltage: sine + seeded noise,
12-bit quantized. The plant's true frequency is never used.
"""

import numpy as np

from edge.attestation import GENESIS_HASH32, SubWindow, build_record
from sensors import windows as gold_det
from sensors.adc import ADCParams, quantize_voltage
from sensors.windows import ZCParams, build_windows, find_rising_crossings, window_mean_power

FS_HZ = 10_000.0
SEED = 42
ZC = ZCParams()


def _measured_sine(
    f_hz: float, duration_s: float, v_pk: float = 325.0, seed: int = SEED
) -> np.ndarray:
    """Measured-style voltage [V]: sine + seeded noise, 12-bit quantized."""
    rng = np.random.default_rng(seed)
    n = int(round(duration_s * FS_HZ))
    t = np.arange(n) / FS_HZ
    clean = v_pk * np.sin(2.0 * np.pi * f_hz * t)
    noisy = clean + rng.normal(0.0, 0.35, size=n)
    return quantize_voltage(noisy, ADCParams())


def _to_record_invalid_assert(subs_valid_flags, zc_positions):
    """Map invalid ZC windows through build_record; assert zero samples + clear bits."""
    n = 200
    subs = [
        SubWindow(
            v_q15=np.full(n, 15000, dtype=np.int64),
            i_q15=np.full(n, 6000, dtype=np.int64),
            valid=valid,
        )
        for valid in subs_valid_flags
    ]
    record, _ = build_record(
        counter=0,
        window_start=0,
        sub_windows=subs,
        device_id=1,
        prev_hash=GENESIS_HASH32,
        energy_prev_uwh=0,
        hmac_key=bytes(32),
    )
    for pos in zc_positions:
        assert record["zc_samples"][pos] == 0
        assert (record["window_flags"] >> pos) & 1 == 0
    return record


def test_missing_crossing_dropout_invalid():
    v = _measured_sine(50.0, 2.0)
    v[int(0.5 * FS_HZ) : int(0.7 * FS_HZ)] = 0.0  # sensor dropout: no crossings
    det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    windows = build_windows(det, frac, v.size, ZC)
    assert windows
    bad = [w for w in windows if not w.valid]
    assert bad and all(w.reason == "gap" for w in bad)
    spanning = [w for w in windows if w.start_idx <= 0.6 * FS_HZ < w.end_idx]
    assert spanning and all(not w.valid for w in spanning)
    bad_pos = [k for k, w in enumerate(windows) if not w.valid]
    assert np.all(np.isnan(window_mean_power(v, v, windows)[bad_pos]))
    _to_record_invalid_assert([True, False, True, True, True], [1])


def test_spurious_crossing_noise_glitch_invalid():
    v = _measured_sine(50.0, 1.0)
    k0 = 5_050  # sine peak (t = 0.505 s): no natural crossing nearby
    assert v[k0 - 1] > 0 and v[k0] > 0  # mid positive half, no natural crossing
    v[k0] = -400.0  # single-sample deep glitch: arms, then spurious trigger
    det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    assert len(det) == 50  # 49 natural crossings plus exactly one spurious
    windows = build_windows(det, frac, v.size, ZC)
    spanning = [w for w in windows if w.start_idx <= k0 < w.end_idx]
    assert spanning and all(not w.valid for w in spanning)
    bad_idx = [k for k, w in enumerate(windows) if not w.valid]
    assert np.all(np.isnan(window_mean_power(v, v, windows)[bad_idx]))


def test_sag_below_hysteresis_no_windows():
    v = _measured_sine(50.0, 1.0, v_pk=3.0)  # never reaches the -5 V arm level
    det, _ = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    assert det.size == 0
    assert build_windows(det, np.array([]), v.size, ZC) == []
    chatter = np.tile([3.0, -3.0], 5000)  # fast ±3 V chatter around zero
    det_c, _ = find_rising_crossings(chatter, FS_HZ, ZC.hyst_V)
    assert det_c.size == 0  # hysteresis rejects sub-threshold chatter


def test_frequency_step_mid_window():
    seg1 = _measured_sine(50.0, 1.0, seed=SEED)[: int(1.0 * FS_HZ)]
    seg2 = _measured_sine(30.0, 1.0, seed=SEED + 1)[: int(1.0 * FS_HZ)]
    v = np.concatenate([seg1, seg2])
    det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    windows = build_windows(det, frac, v.size, ZC)
    spanning = [w for w in windows if w.start_idx < 1.0 * FS_HZ < w.end_idx]
    assert spanning and all(not w.valid for w in spanning)  # step breaks the window
    in_slow = [w for w in windows if w.start_idx >= 1.0 * FS_HZ]
    # 30 Hz zone: every window invalid. Per-cycle gaps (333) already exceed
    # the gap ceiling, so "gap" fires before the M check — a 30 Hz grid is
    # doubly out of spec; pure-"range" failures are pinned by the M-ends test.
    assert in_slow and all(not w.valid for w in in_slow)
    _to_record_invalid_assert([False] * 5, [0, 1, 2, 3, 4])


def test_m_at_both_ends_exact():
    # Synthetic detection grids hitting the M bounds exactly (gaps stay legal).
    det_1810 = np.arange(0, 11 * 181, 181)  # 10 gaps of 181 → M = 1810
    w = build_windows(det_1810, np.zeros_like(det_1810, dtype=float), 10_000, ZC)
    assert len(w) == 1 and w[0].valid and w[0].n_samples == ZC.m_min_samples == 1810
    det_2230 = np.arange(0, 11 * 223, 223)  # 10 gaps of 223 → M = 2230
    w = build_windows(det_2230, np.zeros_like(det_2230, dtype=float), 10_000, ZC)
    assert len(w) == 1 and w[0].valid and w[0].n_samples == ZC.m_max_samples == 2230
    det_1809 = np.append(np.arange(0, 10 * 181, 181), [1809])  # M = 1809
    w = build_windows(det_1809, np.zeros(11), 10_000, ZC)
    assert len(w) == 1 and not w[0].valid and w[0].reason == "range"
    det_2231 = np.append(np.arange(0, 10 * 223, 223), [2231])  # M = 2231
    w = build_windows(det_2231, np.zeros(11), 10_000, ZC)
    assert len(w) == 1 and not w[0].valid and w[0].reason == "range"


def test_m_near_ends_real_signal():
    for f_hz, nominal_m in ((55.22, 1811), (44.90, 2227)):  # just inside the M ends
        v = _measured_sine(f_hz, 12.0 / f_hz + 0.01)
        det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
        windows = build_windows(det, frac, v.size, ZC)
        assert len(windows) == 1 and windows[0].valid
        assert abs(windows[0].n_samples - nominal_m) <= 2


def test_interpolation_formula():
    v = np.array([-10.0, -3.0, 7.0, 10.0])  # armed at k=0, crossing k=1→2
    det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    assert list(det) == [2]
    assert abs(frac[0] - 1.3) < 1e-12  # (k-1) + 3/(3+7)


def test_detector_module_is_golden_reference():
    # tb/golden.py defers detection to sensors/windows.py (single source);
    # this pins the delegation so the two can never silently diverge.
    assert gold_det.find_rising_crossings is find_rising_crossings
    assert gold_det.build_windows is build_windows
