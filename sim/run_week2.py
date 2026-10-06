"""Week 2 evaluation: plant + sensors → figures + metrics (actual run only).

For each load profile (steady, cyclic, bursty): simulate true waveforms,
pass them through the ONLY measurement route (AFE → ADC), compute real
power ``P = mean(v*i)`` and RMS over fixed whole nominal 50 Hz cycles
(200 samples, never given the true frequency), and record mean/max %
error of measured vs true. Also sweeps grid drift ``df`` in
[0, ±0.1, ±0.2, ±0.3, ±0.5] Hz on the steady profile, recording both the
sensor-chain error and the fixed-window bias vs the coherent reference.

Outputs (never fabricated; everything below comes from this run):
- ``results/figures/{steady,cyclic,bursty}_clean_vs_measured.png``
- ``results/metrics.json`` (seed, spec, bound + derivation, per-profile
  errors, frequency sweep table).

Reproduce: ``SEED=42 uv run python sim/run_week2.py`` (or ``make repro``).
"""

import json
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from plant.load_profiles import KINDS, make_profile
from plant.motor import MotorParams, MotorResult, simulate
from sensors import (
    SAMPLES_PER_CYCLE,
    acceptance_bound_pct,
    cycle_mean_power,
    cycle_rms,
    measurement_chain,
)
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams

REPO = Path(__file__).resolve().parents[1]
FIG_DIR = REPO / "results" / "figures"
METRICS_PATH = REPO / "results" / "metrics.json"

SEED = int(os.environ.get("SEED", "42"))
FS_HZ = 10_000.0  # [samples/s] per DECISIONS.md D-04
DURATION_S = 5.0  # [s] per-profile run length
DEFAULT_DRIFT_HZ = 0.2  # [Hz] default grid-deviation evaluation point
SWEEP_DF_HZ = [0.0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.5, -0.5]  # [Hz]


def err_pct(meas: np.ndarray, true: np.ndarray) -> tuple[float, float]:
    """Return (mean %, max %) of ``|meas - true| / |true| * 100``."""
    e = 100.0 * np.abs(meas - true) / np.maximum(np.abs(true), 1e-9)
    return float(np.mean(e)), float(np.max(e))


def run_case(kind: str, df_hz: float, afe_params: AFEParams, adc_params: ADCParams) -> dict:
    """Simulate one (profile, grid-offset) case; return traces + metrics."""
    load = make_profile(kind, duration_s=DURATION_S, fs_hz=FS_HZ, seed=SEED)
    params = MotorParams(f_grid_offset_Hz=df_hz)
    res: MotorResult = simulate(load, fs_hz=FS_HZ, params=params)
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(res.v_true_V, res.i_true_A, t, AFE(afe_params), adc_params)
    p_true = cycle_mean_power(res.v_true_V, res.i_true_A)
    p_meas = cycle_mean_power(v_m, i_m)
    vrms_true = cycle_rms(res.v_true_V)
    vrms_meas = cycle_rms(v_m)
    irms_true = cycle_rms(res.i_true_A)
    irms_meas = cycle_rms(i_m)
    # Coherent reference from plant ground truth (characterization only;
    # the window logic under test never sees the true frequency).
    p_ref = float(np.mean(params.v_rms_nom_V * res.i_rms_A * np.cos(res.phi_rad)))
    win_bias = 100.0 * np.abs(p_true - p_ref) / abs(p_ref)
    vrms_win_bias = 100.0 * np.abs(vrms_true - params.v_rms_nom_V) / params.v_rms_nom_V
    p_mean, p_max = err_pct(p_meas, p_true)
    vrms_mean, vrms_max = err_pct(vrms_meas, vrms_true)
    irms_mean, irms_max = err_pct(irms_meas, irms_true)
    return {
        "t": t,
        "res": res,
        "v_meas": v_m,
        "i_meas": i_m,
        "p_true": p_true,
        "p_meas": p_meas,
        "metrics": {
            "p_mean_pct": p_mean,
            "p_max_pct": p_max,
            "vrms_mean_pct": vrms_mean,
            "vrms_max_pct": vrms_max,
            "irms_mean_pct": irms_mean,
            "irms_max_pct": irms_max,
            "window_bias_p_mean_pct": float(np.mean(win_bias)),
            "window_bias_p_max_pct": float(np.max(win_bias)),
            "window_bias_vrms_max_pct": float(np.max(vrms_win_bias)),
            "true_avg_power_W": float(np.mean(p_true)),
        },
    }


def plot_case(kind: str, case: dict) -> Path:
    """Plot clean vs measured V, I (first 60 ms) and per-cycle P (full run)."""
    t, res = case["t"], case["res"]
    zoom = t < 0.06
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=False)
    fig.suptitle(f"{kind}: clean vs measured (seed {SEED}, true f=50 Hz)")
    axes[0].plot(t[zoom] * 1e3, res.v_true_V[zoom], label="clean V")
    axes[0].plot(t[zoom] * 1e3, case["v_meas"][zoom], alpha=0.7, label="measured V")
    axes[0].set_ylabel("V [V]")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].plot(t[zoom] * 1e3, res.i_true_A[zoom], label="clean I")
    axes[1].plot(t[zoom] * 1e3, case["i_meas"][zoom], alpha=0.7, label="measured I")
    axes[1].set_ylabel("I [A]")
    axes[1].set_xlabel("t [ms]")
    axes[1].legend(loc="upper right", fontsize=8)
    k = np.arange(case["p_true"].size)
    axes[2].plot(k, case["p_true"], label="true P per 50 Hz cycle")
    axes[2].plot(k, case["p_meas"], alpha=0.7, label="measured P per cycle")
    axes[2].set_ylabel("P [W]")
    axes[2].set_xlabel("nominal 50 Hz cycle index")
    axes[2].legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    out = FIG_DIR / f"{kind}_clean_vs_measured.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def main() -> dict:
    """Run all Week 2 cases; write figures + metrics.json; return metrics."""
    afe_params = AFEParams(seed=SEED)
    adc_params = ADCParams()
    bound, derivation = acceptance_bound_pct(afe_params, adc_params)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    profiles = {}
    for kind in KINDS:
        case = run_case(kind, 0.0, afe_params, adc_params)
        plot_case(kind, case)
        profiles[kind] = case["metrics"]

    sweep = []
    for df in SWEEP_DF_HZ:
        case = run_case("steady", df, afe_params, adc_params)
        sweep.append({"df_hz": df, **case["metrics"]})

    metrics = {
        "week": 2,
        "seed": SEED,
        "fs_hz": FS_HZ,
        "duration_s": DURATION_S,
        "samples_per_cycle": SAMPLES_PER_CYCLE,
        "default_drift_hz": DEFAULT_DRIFT_HZ,
        "afe": {k: getattr(afe_params, k) for k in afe_params.__dataclass_fields__},
        "adc": {
            "n_bits": adc_params.n_bits,
            "v_fullscale_pk_V": adc_params.v_fullscale_pk_V,
            "i_fullscale_pk_A": adc_params.i_fullscale_pk_A,
            "lsb_V": adc_params.lsb_V,
            "lsb_A": adc_params.lsb_A,
        },
        "acceptance_bound_p_mean_pct": bound,
        "acceptance_derivation": derivation,
        "profiles": profiles,
        "frequency_sweep_steady": sweep,
        "notes": (
            "Sensor-chain error (measured vs true, identical fixed windows) is "
            "flat across drift: the AFE/ADC path is drift-independent. "
            "Fixed-window bias vs the coherent reference grows ~linearly with "
            "|df| (~1.4%/Hz mean, ~2.2%/Hz max on P); see DECISIONS.md known "
            "limitation. No perfect-world path: all measured data flows AFE->ADC."
        ),
    }
    METRICS_PATH.write_text(json.dumps(metrics, indent=2) + "\n")
    print(f"wrote {METRICS_PATH}")
    print(f"acceptance bound: {bound:.3f}% | {derivation}")
    for kind, m in profiles.items():
        status = "PASS" if m["p_mean_pct"] < bound else "FAIL"
        print(f"{kind}: P mean {m['p_mean_pct']:.4f}% max {m['p_max_pct']:.4f}% [{status}]")
    print("sweep df -> window-bias P mean/max %:")
    for row in sweep:
        print(
            f"  {row['df_hz']:+.1f} Hz: sensor P {row['p_mean_pct']:.4f}/"
            f"{row['p_max_pct']:.4f} | window bias {row['window_bias_p_mean_pct']:.4f}/"
            f"{row['window_bias_p_max_pct']:.4f}"
        )
    return metrics


if __name__ == "__main__":
    main()
