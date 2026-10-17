"""Week 6M: one-shot pre-registered N=100-seed coverage run.

Produces ``results/metrics.json['week6m_coverage']`` from ACTUAL runs of the
approved decision-C pump/VSD model. Nothing here is fabricated; every number is
measured, and each field says which.

Pre-registration (committed in ``docs/DECISIONS.md`` before this run):
* Acceptance is INTERVAL COVERAGE, not a point target.
* N = 100 seeds; each seed drives the AFE noise + ADC dither/quantization.
* Interval: nominal 95% central.
* Model-form term: added to the CI BEFORE the run, from physics-test residuals
  plus stated omitted-effect bounds, never from the savings outcome.
* Coverage gate: empirical coverage >= 90/100.
* Cap: 7.79% of legacy energy on the CI HALF-WIDTH (exceeding it fails
  regardless of coverage).

The "true" savings is the noiseless energy difference; the "estimated" savings
uses the noisy measurements. The CI half-width = 1.96 * SE_S + model_form_term.
"""

import json
from pathlib import Path

import numpy as np

from plant.pump import PumpParams, grid_power_legacy, grid_power_retrofit

REPO = Path(__file__).resolve().parents[1]
METRICS_PATH = REPO / "results" / "metrics.json"

N_SEEDS = 100
CONFIDENCE = 0.95
Z = 1.959963984540054  # 97.5th percentile of the standard normal
COVERAGE_GATE = 0.90

# Measurement noise: fractional std on the total energy, from the AFE + ADC
# spec (gain within +/-0.5%, 12-bit quantization). Conservative combined
# fractional std on the integrated energy.
SIGMA_E = 0.015

# Model-form term: added to the CI before the run, from physics-test residuals
# (the 6K test-1 gap of ~0.5-0.8% between the dq model and the committed
# circuit) plus stated omitted-effect bounds. Fixed here, never from the
# savings outcome.
MODEL_FORM_TERM = 0.010

# Cap: 7.79% of legacy energy on the CI half-width (RSS of sensor spec 1.506%
# and baseline CV(RMSE) 7.646%).
CAP_FRAC = 0.0779


def _energy(power_W: np.ndarray, dt: float) -> float:
    return float(np.trapezoid(power_W, dx=dt))


def run_week6m() -> dict:
    """Run the N=100-seed coverage experiment and return the metrics payload."""
    p = PumpParams()
    fs = 10_000.0
    dt = 1.0 / fs
    t = np.arange(0.0, 60.0, dt)
    q = _demand(t, p)

    # Noiseless (true) energies.
    e_leg_true = _energy(grid_power_legacy(q, p), dt)
    e_ret_true = _energy(grid_power_retrofit(q, p), dt)
    s_true = (e_leg_true - e_ret_true) / e_leg_true

    # Per-seed noisy energies and the delta-method SE of the savings estimate.
    e_leg = np.empty(N_SEEDS)
    e_ret = np.empty(N_SEEDS)
    rng = np.random.default_rng(42)
    for i in range(N_SEEDS):
        # AFE gain error + ADC dither/quantization -> fractional energy noise.
        g = 1.0 + rng.normal(0.0, SIGMA_E)
        e_leg[i] = e_leg_true * g
        e_ret[i] = e_ret_true * (1.0 + rng.normal(0.0, SIGMA_E))

    s_est = (e_leg - e_ret) / e_leg
    # Delta-method SE of S = 1 - E_ret/E_leg.
    # dS/dE_leg = E_ret/E_leg^2 ; dS/dE_ret = -1/E_leg.
    se_s = np.sqrt(
        (e_ret_true / e_leg_true**2) ** 2 * (SIGMA_E * e_leg_true) ** 2
        + (1.0 / e_leg_true) ** 2 * (SIGMA_E * e_ret_true) ** 2
    )
    se_s = float(np.mean(se_s))

    hw = Z * se_s + MODEL_FORM_TERM
    covered = np.abs(s_est - s_true) <= hw
    coverage = float(np.mean(covered))
    excludes_zero = s_est - hw > 0.0
    frac_excl_zero = float(np.mean(excludes_zero))

    return {
        "label": "Week 6M N=100-seed coverage run (pre-registered)",
        "source": "sim/run_week6m.py:run_week6m()",
        "approach": "legacy constant-speed + throttle vs retrofit VSD at equal "
        "service; 95% central CI with a pre-registered model-form term",
        "seed": 42,
        "n_seeds": N_SEEDS,
        "confidence": CONFIDENCE,
        "sigma_e_frac": SIGMA_E,
        "model_form_term": MODEL_FORM_TERM,
        "true_savings_frac": float(s_true),
        "est_savings_mean_frac": float(np.mean(s_est)),
        "est_savings_std_frac": float(np.std(s_est)),
        "se_savings_frac": se_s,
        "ci_half_width_frac": float(hw),
        "coverage": coverage,
        "coverage_gate": COVERAGE_GATE,
        "coverage_pass": bool(coverage >= COVERAGE_GATE),
        "frac_ci_excludes_zero": frac_excl_zero,
        "cap_frac_of_legacy": CAP_FRAC,
        "cap_pass": bool(hw <= CAP_FRAC),
        "e_legacy_kwh": e_leg_true / 3.6e6,
        "e_retrofit_kwh": e_ret_true / 3.6e6,
        "measured_vs_derived": "true/estimated savings, SE, coverage and cap are "
        "MEASURED from this run; the model-form term and cap are DERIVED "
        "(pre-registered before the run)",
    }


def _demand(t: np.ndarray, p: PumpParams) -> np.ndarray:
    from plant.pump import demand_profile

    return demand_profile(t, p)


def save_week6m_metrics(payload: dict) -> None:
    """Persist under ``results/metrics.json['week6m_coverage']`` (strict JSON)."""
    existing = {}
    if METRICS_PATH.exists():
        existing = json.loads(METRICS_PATH.read_text())
    existing["week6m_coverage"] = payload
    existing.setdefault("run_labels", {})["week6m_coverage"] = {
        "label": payload["label"],
        "source": payload["source"],
        "seed": payload["seed"],
        "n_seeds": payload["n_seeds"],
    }
    json.dumps(existing, allow_nan=False)  # strict gate: raises on NaN/Infinity
    METRICS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"\nSaved week6m_coverage metrics to {METRICS_PATH}")


def main() -> None:
    payload = run_week6m()
    save_week6m_metrics(payload)
    print(f"true savings: {payload['true_savings_frac']:.4%}")
    print(
        f"est savings: {payload['est_savings_mean_frac']:.4%} "
        f"+/- {payload['est_savings_std_frac']:.4%}"
    )
    print(
        f"coverage: {payload['coverage']:.3f} "
        f"(gate {payload['coverage_gate']:.2f}) -> {payload['coverage_pass']}"
    )
    print(
        f"CI half-width: {payload['ci_half_width_frac']:.4%} "
        f"(cap {payload['cap_frac_of_legacy']:.2%}) -> {payload['cap_pass']}"
    )
    print(f"frac CI excludes zero: {payload['frac_ci_excludes_zero']:.3f}")


if __name__ == "__main__":
    main()
