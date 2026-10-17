"""Week 6M tests: N=100-seed coverage run (pre-registered acceptance).

The acceptance setup (N=100, 95% central interval, model-form term added before
the run, coverage gate >= 90/100, cap = 7.79% of legacy energy on the CI
half-width) was pre-registered in ``docs/DECISIONS.md`` before this run. These
tests encode the "done when" criteria and the bookkeeping invariants.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from plant.pump import (
    PumpParams,
    demand_profile,
    grid_power_legacy,
    grid_power_retrofit,
    hydraulic_energy,
    solve_speed,
    system_head,
)
from sim.run_week6m import (
    CAP_FRAC,
    COVERAGE_GATE,
    MODEL_FORM_TERM,
    N_SEEDS,
    SIGMA_E,
    run_week6m,
)

REPO = Path(__file__).resolve().parents[1]
METRICS_PATH = REPO / "results" / "metrics.json"


class TestPumpPhysics:
    """Sanity of the approved decision-C curves (independent of the run)."""

    def test_design_point_on_both_curves(self):
        p = PumpParams()
        h_sys = system_head(p.q_design_m3s, p)
        assert h_sys == pytest.approx(p.h_design_m, rel=1e-9)

    def test_solve_speed_returns_nominal_at_design(self):
        p = PumpParams()
        n = solve_speed(p.q_design_m3s, p)
        assert n == pytest.approx(p.n_nom_Hz, rel=1e-9)

    def test_retrofit_speed_below_nominal_at_partial_flow(self):
        p = PumpParams()
        q = 0.5 * p.q_design_m3s
        n = solve_speed(q, p)
        assert 0.0 < n < p.n_nom_Hz

    def test_hydraulic_energy_positive(self):
        p = PumpParams()
        t = np.linspace(0.0, 60.0, 6001)
        q = demand_profile(t, p)
        e = hydraulic_energy(q, np.full_like(q, p.h_design_m), 0.01, p)
        assert e > 0.0


class TestCoverageRun:
    """The one-shot pre-registered run and its acceptance result."""

    def test_seed_count_and_noise_are_pre_registered(self):
        assert N_SEEDS == 100
        assert SIGMA_E > 0.0
        assert MODEL_FORM_TERM > 0.0

    def test_cap_is_seven_point_seven_nine_percent(self):
        # RSS of sensor spec 1.506% and baseline CV(RMSE) 7.646%
        expected = (0.01506**2 + 0.07646**2) ** 0.5
        assert CAP_FRAC == pytest.approx(expected, rel=1e-3)

    def test_coverage_gate(self):
        assert COVERAGE_GATE == 0.90

    def test_run_returns_self_consistent_numbers(self):
        r = run_week6m()
        # The estimate is unbiased up to noise: |mean_est - true| small relative
        # to the standard error of the mean.
        sem = r["est_savings_std_frac"] / np.sqrt(N_SEEDS)
        assert abs(r["est_savings_mean_frac"] - r["true_savings_frac"]) < 5 * sem
        # CI half-width exceeds the pure-noise term (model-form term added).
        assert r["ci_half_width_frac"] > 1.959963984540054 * r["se_savings_frac"]

    def test_coverage_and_cap_pass(self):
        r = run_week6m()
        assert r["coverage"] >= COVERAGE_GATE, (
            f"coverage {r['coverage']:.3f} < gate {COVERAGE_GATE:.2f}"
        )
        assert r["ci_half_width_frac"] <= CAP_FRAC, (
            f"CI half-width {r['ci_half_width_frac']:.4%} > cap {CAP_FRAC:.2%}"
        )


class TestMetricsArtifact:
    """results/metrics.json['week6m_coverage'] must be strict JSON and complete."""

    @staticmethod
    def _payload():
        return json.loads(METRICS_PATH.read_text())["week6m_coverage"]

    def test_strict_json_and_required_keys(self):
        k = self._payload()
        for key in (
            "n_seeds",
            "confidence",
            "true_savings_frac",
            "est_savings_mean_frac",
            "coverage",
            "coverage_gate",
            "coverage_pass",
            "frac_ci_excludes_zero",
            "ci_half_width_frac",
            "cap_frac_of_legacy",
            "cap_pass",
        ):
            assert key in k, key

    def test_recorded_values_match_the_run(self):
        k = self._payload()
        r = run_week6m()
        assert k["n_seeds"] == r["n_seeds"]
        assert k["coverage"] == pytest.approx(r["coverage"], abs=1e-12)
        assert k["coverage_pass"] == r["coverage_pass"]
        assert k["cap_pass"] == r["cap_pass"]
        assert k["cap_frac_of_legacy"] == pytest.approx(CAP_FRAC, rel=1e-3)

    def test_artifact_is_consistent_with_strict_json(self):
        # Re-serialize with allow_nan=False: raises if any NaN/Infinity slipped in.
        json.dumps(self._payload(), allow_nan=False)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
