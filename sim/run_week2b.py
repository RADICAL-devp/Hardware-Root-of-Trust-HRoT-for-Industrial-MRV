"""Week 2b evaluation: zero-crossing windows vs fixed windows under stress.

Stress conditions (always WITH measurement noise — no perfect-world path):
3rd/5th voltage harmonics at 3 %/1 % of Vpk plus a 2.0 V plant DC offset,
frequency sweep ``df`` in [0, ±0.1, ±0.2, ±0.3, ±0.5] Hz on the steady
profile. Cycle boundaries for the ZC path come ONLY from rising zero
crossings detected on the measured, quantized voltage; the true frequency
is never used except in the offline coherent reference (plant ground
truth, characterization only).

Appends (never rewrites) the ``zc_windows`` section to
``results/metrics.json`` next to the Week 2 fixed-window numbers, and
writes ``results/figures/zc_vs_fixed_sweep.png``.

Reproduce: ``SEED=42 uv run python sim/run_week2b.py``.
"""

import json
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from plant.load_profiles import make_profile
from plant.motor import MotorParams, MotorResult, simulate
from sensors import SAMPLES_PER_CYCLE, cycle_mean_power, measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams
from sensors.windows import (
    ZCParams,
    build_windows,
    find_rising_crossings,
    reciprocal_lut_inv,
    window_mean_power,
)

REPO = Path(__file__).resolve().parents[1]
FIG_PATH = REPO / "results" / "figures" / "zc_vs_fixed_sweep.png"
METRICS_PATH = REPO / "results" / "metrics.json"

SEED = int(os.environ.get("SEED", "42"))
FS_HZ = 10_000.0  # [samples/s]
DURATION_S = 5.0  # [s]
SWEEP_DF_HZ = [0.0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.5, -0.5]  # [Hz]
STRESS = {"v_harm3_frac": 0.03, "v_harm5_frac": 0.01, "v_dc_V": 2.0}


def coherent_pref(params: MotorParams, res: MotorResult) -> float:
    """Analytic average power [W] incl. harmonic active power (reference only)."""
    v_pk = params.v_rms_nom_V * np.sqrt(2.0)
    p = params.v_rms_nom_V * res.i_rms_A * np.cos(res.phi_rad)
    p = p + (STRESS["v_harm3_frac"] * v_pk / np.sqrt(2.0)) * res.i_h3_rms_A * np.cos(res.phi_h3_rad)
    p = p + (STRESS["v_harm5_frac"] * v_pk / np.sqrt(2.0)) * res.i_h5_rms_A * np.cos(res.phi_h5_rad)
    return float(np.mean(p))


def run_point(df_hz: float, zc: ZCParams) -> dict:
    """Run one sweep point; return fixed-window vs ZC-window errors [%]."""
    load = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=SEED)
    params = MotorParams(f_grid_offset_Hz=df_hz, **STRESS)
    res = simulate(load, fs_hz=FS_HZ, params=params)
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    pref = coherent_pref(params, res)
    # Fixed 200-sample windows on the TRUE waveforms (window bias, no sensor).
    p_win = cycle_mean_power(res.v_true_V, res.i_true_A, SAMPLES_PER_CYCLE)
    fixed_bias = 100.0 * np.abs(p_win - pref) / abs(pref)
    # Sensor-chain error under identical fixed windows (drift-independent check).
    p_meas = cycle_mean_power(v_m, i_m, SAMPLES_PER_CYCLE)
    sensor_err = 100.0 * np.abs(p_meas - p_win) / np.abs(p_win)
    # ZC windows from measured quantized voltage only.
    det_idx, frac = find_rising_crossings(v_m, FS_HZ, zc.hyst_V)
    windows = build_windows(det_idx, frac, v_m.size, zc)
    p_zc = window_mean_power(v_m, i_m, windows)
    zc_err = 100.0 * np.abs(p_zc - pref) / abs(pref)
    valid_frac = float(np.mean([w.valid for w in windows])) if windows else 0.0
    return {
        "df_hz": df_hz,
        "fixed_p_mean_pct": float(np.mean(fixed_bias)),
        "fixed_p_max_pct": float(np.max(fixed_bias)),
        "sensor_p_mean_pct": float(np.mean(sensor_err)),
        "sensor_p_max_pct": float(np.max(sensor_err)),
        "zc_p_mean_pct": float(np.nanmean(zc_err)),
        "zc_p_max_pct": float(np.nanmax(zc_err)),
        "zc_valid_frac": valid_frac,
        "zc_n_windows": len(windows),
        "true_avg_power_W": pref,
    }


def lut_worst_pct(zc: ZCParams) -> float:
    """Worst-case relative error [%] of the reciprocal LUT over the M range."""
    worst = 0.0
    for m in range(zc.m_min_samples, zc.m_max_samples + 1):
        lut = reciprocal_lut_inv(m, zc.m_min_samples, zc.lut_out_bits)
        rel = abs(lut / float(1 << zc.lut_out_bits) - 1.0 / m) / (1.0 / m)
        worst = max(worst, rel)
    return worst * 100.0


def plot_sweep(rows: list[dict]) -> None:
    """Plot fixed-window bias vs ZC residual across |df| (improvement visible)."""
    rows_sorted = sorted(rows, key=lambda r: abs(r["df_hz"]))
    x = [abs(r["df_hz"]) for r in rows_sorted]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(x, [r["fixed_p_max_pct"] for r in rows_sorted], "o-", label="fixed max")
    ax.plot(x, [r["fixed_p_mean_pct"] for r in rows_sorted], "s--", label="fixed mean")
    ax.plot(x, [r["zc_p_max_pct"] for r in rows_sorted], "o-", label="zero-crossing max")
    ax.plot(x, [r["zc_p_mean_pct"] for r in rows_sorted], "s--", label="zero-crossing mean")
    ax.set_xlabel("|grid frequency offset| [Hz]")
    ax.set_ylabel("P error vs coherent reference [%]")
    ax.set_title("Fixed vs zero-crossing windows (noise + 3 %/1 % harmonics + 2 V DC)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG_PATH, dpi=120)
    plt.close(fig)


def main() -> dict:
    """Run the stress sweep; merge ``zc_windows`` into metrics.json."""
    zc = ZCParams()
    rows = [run_point(df, zc) for df in SWEEP_DF_HZ]
    plot_sweep(rows)
    section = {
        "n_cycles": zc.n_cycles,
        "hyst_V": zc.hyst_V,
        "f_min_Hz": zc.f_min_Hz,
        "f_max_Hz": zc.f_max_Hz,
        "gap_min_samples": zc.gap_min_samples,
        "gap_max_samples": zc.gap_max_samples,
        "m_min_samples": zc.m_min_samples,
        "m_max_samples": zc.m_max_samples,
        "lut_out_bits": zc.lut_out_bits,
        "lut_max_rel_err_pct": lut_worst_pct(zc),
        "plant_stress": dict(STRESS),
        "stress_sweep": rows,
        "notes": (
            "Detector input is measured quantized voltage only. Fixed-window "
            "bias grows with |df| under stress; ZC residual stays at the "
            "integer-boundary truncation + noise floor. Invalid windows yield "
            "NaN and are excluded (flagged upstream via window_flags)."
        ),
    }
    metrics = json.loads(METRICS_PATH.read_text())
    metrics["zc_windows"] = section
    METRICS_PATH.write_text(json.dumps(metrics, indent=2) + "\n")
    print(f"merged zc_windows into {METRICS_PATH}; figure {FIG_PATH}")
    for r in rows:
        print(
            f"df {r['df_hz']:+.1f}: fixed {r['fixed_p_mean_pct']:.4f}/"
            f"{r['fixed_p_max_pct']:.4f} | zc {r['zc_p_mean_pct']:.4f}/"
            f"{r['zc_p_max_pct']:.4f} | valid {r['zc_valid_frac']:.2f}"
        )
    return section


if __name__ == "__main__":
    main()
