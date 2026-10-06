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
from sensors.windows import (
    ZCParams,
    build_windows,
    find_rising_crossings,
    find_rising_crossings_q15,
    window_mean_power,
)

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
    # Integer-domain formula: codes are round(v/500·32768), so the volts
    # [-10, -3, 7] straddle becomes codes [-655, -197, 459]; the fraction
    # is computed on codes, never on volts.
    v = np.array([-10.0, -3.0, 7.0, 10.0])  # armed at k=0, crossing k=1→2
    det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    assert list(det) == [2]
    assert abs(frac[0] - (1 + 197 / 656)) < 1e-12
    det_q, frac_q = find_rising_crossings_q15(np.array([-655, -197, 459, 655]))
    assert list(det_q) == [2] and abs(frac_q[0] - (1 + 197 / 656)) < 1e-12


def test_interpolation_accuracy_measured_not_assumed():
    # Integer-boundary truncation (the RTL contract) vs fractional-endpoint
    # correction (diagnostics only): measured on a +0.5 Hz drifted signal
    # with the Week 2b stress bundle. Both residuals sit at the sensor
    # floor (~0.10 %); interpolation buys ~0.01 pp — not worth RTL cost.
    from plant.load_profiles import make_profile
    from plant.motor import MotorParams, simulate
    from sensors import measurement_chain
    from sensors.adc import ADCParams
    from sensors.afe import AFE, AFEParams
    from tb.golden import fractional_window_mean, window_power

    load = make_profile("steady", duration_s=5.0, fs_hz=FS_HZ, seed=SEED)
    params = MotorParams(f_grid_offset_Hz=0.5, v_harm3_frac=0.03, v_harm5_frac=0.01, v_dc_V=2.0)
    res = simulate(load, fs_hz=FS_HZ, params=params)
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    from edge.framing import q15_encode

    vq = [q15_encode(x, 500.0) for x in v_m]
    iq = [q15_encode(x, 100.0) for x in i_m]
    det, frac = find_rising_crossings(v_m, FS_HZ, ZC.hyst_V)
    windows = build_windows(det, frac, v_m.size, ZC)
    assert windows and all(w.valid for w in windows)
    v_pk = params.v_rms_nom_V * np.sqrt(2.0)
    pref = float(
        np.mean(
            params.v_rms_nom_V * res.i_rms_A * np.cos(res.phi_rad)
            + (0.03 * v_pk / np.sqrt(2.0)) * res.i_h3_rms_A * np.cos(res.phi_h3_rad)
            + (0.01 * v_pk / np.sqrt(2.0)) * res.i_h5_rms_A * np.cos(res.phi_h5_rad)
        )
    )
    det_list, frac_list = det.tolist(), frac.tolist()
    err_int, err_frac = [], []
    for w in windows:
        g = window_power(vq[w.start_idx : w.end_idx], iq[w.start_idx : w.end_idx])
        err_int.append(abs(g.p_avg_q30 / 2**30 * 50000.0 - pref) / abs(pref) * 100.0)
        j = det_list.index(w.start_idx)
        f = fractional_window_mean(vq, iq, frac_list[j], frac_list[j + 10])
        err_frac.append(abs(f / 2**30 * 50000.0 - pref) / abs(pref) * 100.0)
    assert max(err_int) <= 0.20  # truncation cost bounded; cf. D-05 0.13-0.14 %
    assert max(err_frac) <= max(err_int)  # correction helps, marginally
    print(f"\ninteger max {max(err_int):.4f} % vs fractional max {max(err_frac):.4f} %")


def test_thresholds_are_intended_rounding_of_float_spec():
    # Pre-4a2 float rules: arm `v < -5.0`, trigger `v[k-1] < 0.0 <= v[k]`.
    # Integer rules are their intended rounding to Q15 codes — stated here
    # so the mapping is reviewable, not buried in an expression.
    from sensors.windows import ARM_Q15, TRIG_Q15

    assert ARM_Q15 == -328 == round(-5.0 / 500.0 * 32768.0)
    assert TRIG_Q15 == 0 == round(0.0 / 500.0 * 32768.0)


def test_detector_module_is_golden_reference():
    # tb/golden.py defers detection to sensors/windows.py (single source);
    # this pins the delegation so the two can never silently diverge.
    assert gold_det.find_rising_crossings is find_rising_crossings
    assert gold_det.build_windows is build_windows


def test_q15_map_matches_framing_all_codes():
    from edge.framing import q15_encode
    from sensors.windows import _volts_to_q15

    adc = ADCParams()
    for code in range(4096):  # exhaustive over the ADC, not a sample
        v = code * adc.lsb_V - adc.v_fullscale_pk_V
        assert int(_volts_to_q15(np.array([v]))[0]) == q15_encode(v, 500.0)


def test_wrapper_equals_integer_core_seeded_set():
    from edge.framing import q15_encode
    from sensors.windows import find_rising_crossings_q15

    rng = np.random.default_rng(SEED)
    checked = 0
    for trial in range(200):  # freq/amplitude/harmonics/DC/noise/dropouts
        f_hz = float(rng.uniform(44.0, 56.0))
        v_pk = float(rng.uniform(100.0, 400.0))
        h3, h5 = float(rng.uniform(0.0, 0.03)), float(rng.uniform(0.0, 0.01))
        v_dc = float(rng.uniform(-5.0, 5.0))
        n = int(rng.integers(2000, 6000))
        t = np.arange(n) / FS_HZ
        phase = 2.0 * np.pi * f_hz * t
        clean = v_pk * np.sin(phase) + h3 * v_pk * np.sin(3 * phase) + h5 * v_pk * np.sin(5 * phase)
        clean = clean + v_dc
        noisy = clean + rng.normal(0.0, 0.35, size=n)
        v = quantize_voltage(noisy, ADCParams())
        if trial % 5 == 0:  # every fifth signal carries a dropout
            v[n // 3 : n // 3 + 400] = 0.0
        det_a, _ = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
        codes = np.array([q15_encode(x, 500.0) for x in v], dtype=np.int64)
        det_b, _ = find_rising_crossings_q15(codes)
        np.testing.assert_array_equal(det_a, det_b)
        checked += 1
    assert checked == 200


def test_frac_never_decides_edges():
    v = _measured_sine(50.3, 2.0)
    det, frac = find_rising_crossings(v, FS_HZ, ZC.hyst_V)
    w1 = build_windows(det, frac, v.size, ZC)
    w2 = build_windows(det, -999.0 * frac + 17.0, v.size, ZC)  # garbage fractions
    assert [(w.start_idx, w.end_idx, w.valid) for w in w1] == [
        (w.start_idx, w.end_idx, w.valid) for w in w2
    ]
