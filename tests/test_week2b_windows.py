"""Week 2b: zero-crossing-aligned windows (D-05 mitigation).

The detector under test sees ONLY the measured, quantized voltage Winvalid.
windows must be flagged, never silently healed.
"""

import numpy as np

from plant.load_profiles import make_profile
from plant.motor import MotorParams, simulate
from sensors import SAMPLES_PER_CYCLE, acceptance_bound_pct, cycle_mean_power, measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams
from sensors.windows import (
    ZCParams,
    build_windows,
    find_rising_crossings,
    reciprocal_lut_inv,
    window_mean_power,
)

FS_HZ = 10_000.0
DURATION_S = 5.0
SEED = 42
# Stress conditions for the sweep: 3rd/5th harmonics + plant DC offset,
# always WITH measurement noise (no perfect-world path).
STRESS = {"v_harm3_frac": 0.03, "v_harm5_frac": 0.01, "v_dc_V": 2.0}


def _stress_case(df_hz: float):
    load = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=SEED)
    params = MotorParams(f_grid_offset_Hz=df_hz, **STRESS)
    res = simulate(load, fs_hz=FS_HZ, params=params)
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    return params, res, v_m, i_m


def _coherent_pref(params: MotorParams, res) -> float:
    """Analytic average power [W] from plant ground truth (characterization only)."""
    v_pk = params.v_rms_nom_V * np.sqrt(2.0)
    p = params.v_rms_nom_V * res.i_rms_A * np.cos(res.phi_rad)
    p = p + (STRESS["v_harm3_frac"] * v_pk / np.sqrt(2.0)) * res.i_h3_rms_A * np.cos(res.phi_h3_rad)
    p = p + (STRESS["v_harm5_frac"] * v_pk / np.sqrt(2.0)) * res.i_h5_rms_A * np.cos(res.phi_h5_rad)
    return float(np.mean(p))


def _zc_errors(df_hz: float):
    params, res, v_m, i_m = _stress_case(df_hz)
    zc = ZCParams()
    det_idx, frac = find_rising_crossings(v_m, FS_HZ, zc.hyst_V)
    windows = build_windows(det_idx, frac, v_m.size, zc)
    p_zc = window_mean_power(v_m, i_m, windows)
    pref = _coherent_pref(params, res)
    err = 100.0 * np.abs(p_zc - pref) / abs(pref)
    valid_frac = float(np.mean([w.valid for w in windows])) if windows else 0.0
    return float(np.mean(err)), float(np.max(err)), valid_frac, len(windows)


def _fixed_bias(df_hz: float):
    params, res, _, _ = _stress_case(df_hz)
    p_win = cycle_mean_power(res.v_true_V, res.i_true_A, SAMPLES_PER_CYCLE)
    pref = _coherent_pref(params, res)
    bias = 100.0 * np.abs(p_win - pref) / abs(pref)
    return float(np.mean(bias)), float(np.max(bias))


def test_crossings_found_and_track_true_frequency():
    # Detector input is measured quantized voltage only; the true frequency
    # is used here SOLELY to state the expectation, never fed to the detector.
    for df in (0.0, 0.5, -0.5):
        f_true = 50.0 + df
        _, _, v_m, _ = _stress_case(df)
        det_idx, _ = find_rising_crossings(v_m, FS_HZ, ZCParams().hyst_V)
        assert abs(len(det_idx) - f_true * DURATION_S) <= 2
        gaps = np.diff(det_idx)
        assert abs(float(np.mean(gaps)) - FS_HZ / f_true) < 1.0


def test_zc_beats_fixed_under_stress():
    bound, _ = acceptance_bound_pct(AFEParams(), ADCParams())
    for df in (0.5, -0.5):
        fixed_mean, fixed_max = _fixed_bias(df)
        zc_mean, zc_max, valid_frac, n_win = _zc_errors(df)
        assert n_win >= 20  # 5 s at ~50 Hz yields ~24 ten-cycle windows
        assert valid_frac == 1.0  # no dropouts in these runs: all windows valid
        assert zc_max < 0.5 * fixed_max, (df, zc_max, fixed_max)
        assert zc_mean < 0.5 * fixed_mean, (df, zc_mean, fixed_mean)
        assert zc_max < bound, (df, zc_max, bound)


def test_invalid_window_on_dropout():
    params, res, v_m, i_m = _stress_case(0.0)
    v_gap = v_m.copy()
    v_gap[int(1.0 * FS_HZ) : int(1.5 * FS_HZ)] = 0.0  # sensor dropout: no crossings
    zc = ZCParams()
    det_idx, frac = find_rising_crossings(v_gap, FS_HZ, zc.hyst_V)
    windows = build_windows(det_idx, frac, v_gap.size, zc)
    assert windows, "expected windows outside the dropout"
    invalid = [w for w in windows if not w.valid]
    valid = [w for w in windows if w.valid]
    assert invalid, "windows overlapping the dropout must be flagged invalid"
    assert valid, "windows clear of the dropout must stay valid"
    p = window_mean_power(v_gap, i_m, windows)
    for k, w in enumerate(windows):
        if not w.valid:
            assert np.isnan(p[k]), "invalid windows must yield NaN, never a number"
    _ = (params, res)  # ground truth unused here by construction


def test_out_of_range_frequency_all_invalid():
    # 40 Hz is outside the supported 45–55 Hz range: window sample counts
    # exceed M_MAX, so every window must be flagged invalid.
    load = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=SEED)
    res = simulate(load, fs_hz=FS_HZ, params=MotorParams(f_grid_offset_Hz=-10.0))
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, _ = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    zc = ZCParams()
    det_idx, frac = find_rising_crossings(v_m, FS_HZ, zc.hyst_V)
    assert len(det_idx) > 0  # crossings still detected; the RANGE check rejects
    windows = build_windows(det_idx, frac, v_m.size, zc)
    assert windows
    assert all(not w.valid for w in windows)


def test_reciprocal_lut_accuracy_cost():
    # Variable-count mean needs 1/M in RTL: the sized 24-bit reciprocal LUT
    # over the full supported M range must cost far less than the sensor
    # budget. (An 18-bit LUT was measured at 0.41 % — material, rejected.)
    zc = ZCParams()
    worst = 0.0
    m = zc.m_min_samples
    while m <= zc.m_max_samples:
        lut = reciprocal_lut_inv(m, zc.m_min_samples, zc.lut_out_bits)
        rel = abs(lut / float(1 << zc.lut_out_bits) - 1.0 / m) / (1.0 / m)
        worst = max(worst, rel)
        m += 1
    assert worst * 100.0 < 0.01  # <%: ~230x below the 1.506 % sensor bound


def test_stress_conditions_present():
    # Sanity: the sweep really runs with harmonics + DC offset (else the
    # robustness claim would be vacuous).
    params, res, _, _ = _stress_case(0.0)
    v = res.v_true_V
    assert abs(float(np.mean(v)) - STRESS["v_dc_V"]) < 0.5
    theta = 2.0 * np.pi * res.f_grid_Hz * res.t_s
    amp3 = 2.0 * float(np.mean(v * np.sin(3.0 * theta)))
    v_pk = params.v_rms_nom_V * np.sqrt(2.0)
    assert abs(amp3 / v_pk - STRESS["v_harm3_frac"]) < 0.005
