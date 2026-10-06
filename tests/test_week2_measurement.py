"""Week 2: measurement error bound, no-bypass rule, and grid-frequency sweep.

- P is computed over whole nominal 50 Hz cycles (fixed 200-sample windows)
  from measured V/I vs true V/I; mean/max % error per profile must sit under
  an acceptance bound derived from the sensor spec (not tuned to pass).
- No code route may bypass the AFE: the plant must not import sensors, and
  measured signals must differ from clean ones when noise is enabled.
- Frequency sweep: fixed-window error must grow with |df|; results feed
  metrics.json via sim/run_week2.py (this test checks the trend only).
"""

from pathlib import Path

import numpy as np

from plant.load_profiles import make_profile
from plant.motor import MotorParams, simulate
from sensors import (
    SAMPLES_PER_CYCLE,
    acceptance_bound_pct,
    cycle_mean_power,
    cycle_rms,
    measurement_chain,
)
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams

FS_HZ = 10_000.0
DURATION_S = 5.0
SEED = 42


def _measured_vs_true(kind, df_hz=0.0):
    load = make_profile(kind, duration_s=DURATION_S, fs_hz=FS_HZ, seed=SEED)
    params = MotorParams(f_grid_offset_Hz=df_hz)
    res = simulate(load, fs_hz=FS_HZ, params=params)
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    p_true = cycle_mean_power(res.v_true_V, res.i_true_A, SAMPLES_PER_CYCLE)
    p_meas = cycle_mean_power(v_m, i_m, SAMPLES_PER_CYCLE)
    err_pct = 100.0 * np.abs(p_meas - p_true) / np.abs(p_true)
    return float(err_pct.mean()), float(err_pct.max())


def test_measurement_error_under_spec_derived_bound():
    bound, _derivation = acceptance_bound_pct(AFEParams(), ADCParams())
    assert bound > 0
    for kind in ("steady", "cyclic", "bursty"):
        mean_pct, max_pct = _measured_vs_true(kind)
        assert mean_pct < bound, (kind, mean_pct, bound)
        assert max_pct < 3.0 * bound, (kind, max_pct, bound)


def test_no_perfect_world_path():
    # 1. Plant modules must not reach into sensors (no bypass import).
    repo = Path(__file__).resolve().parents[1]
    for mod in ("motor.py", "thermal.py", "load_profiles.py"):
        src = (repo / "plant" / mod).read_text()
        assert "sensors" not in src, mod
        assert "afe" not in src.lower().replace("safe", ""), mod
    # 2. With noise enabled, the chain output must differ from clean input.
    load = make_profile("steady", duration_s=1.0, fs_hz=FS_HZ, seed=SEED)
    res = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    assert not np.array_equal(v_m, res.v_true_V)
    assert not np.array_equal(i_m, res.i_true_A)


def _window_bias_vs_coherent(df_hz: float) -> tuple[float, float, float]:
    """Fixed-window bias vs the coherent (true-frequency) power reference.

    The reference is the analytic average power from the plant's own
    quasi-static circuit state (``Vrms * Irms * cos(phi)`` averaged over the
    run). It uses plant ground truth, never the sensor path; the window
    logic under test still assumes exactly 50 Hz. Returns
    (P bias mean %, P bias max %, Vrms window-bias max %).
    """
    load = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=SEED)
    params = MotorParams(f_grid_offset_Hz=df_hz)
    res = simulate(load, fs_hz=FS_HZ, params=params)
    p_ref = float(np.mean(params.v_rms_nom_V * res.i_rms_A * np.cos(res.phi_rad)))
    p_win = cycle_mean_power(res.v_true_V, res.i_true_A, SAMPLES_PER_CYCLE)
    bias = 100.0 * np.abs(p_win - p_ref) / abs(p_ref)
    vrms_win = cycle_rms(res.v_true_V, SAMPLES_PER_CYCLE)
    vrms_bias = 100.0 * np.abs(vrms_win - params.v_rms_nom_V) / params.v_rms_nom_V
    return float(bias.mean()), float(bias.max()), float(vrms_bias.max())


def test_frequency_offset_grows_whole_cycle_error():
    # Fixed 200-sample windows assume exactly 50 Hz (true freq is NEVER
    # passed to the window logic). The sensor-chain error (measured vs true
    # under identical windows) stays under the spec bound across the sweep,
    # while the fixed-window bias vs the coherent reference grows with |df|.
    bound, _ = acceptance_bound_pct(AFEParams(), ADCParams())
    for df in (-0.5, -0.2, 0.0, 0.2, 0.5):
        mean_pct, max_pct = _measured_vs_true("steady", df_hz=df)
        assert mean_pct < bound, (df, mean_pct, bound)
        assert max_pct < 3.0 * bound, (df, max_pct, bound)
    bias_0, _, _ = _window_bias_vs_coherent(0.0)
    bias_02, _, _ = _window_bias_vs_coherent(0.2)
    bias_05, max_05, vrms_max_05 = _window_bias_vs_coherent(0.5)
    assert bias_0 < 0.01  # coherent at nominal frequency
    assert bias_02 > bias_0  # drift introduces bias
    assert bias_05 > bias_02  # bias grows with |df|
    assert bias_05 > 0.3  # material: same order as the sensor error budget
    assert max_05 > 0.5
    assert vrms_max_05 > 0.2
