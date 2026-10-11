"""Week 6C JSON validity regression tests (review closeout).

results/metrics.json must be strict-standard JSON: no NaN/Infinity tokens.
Undefined relative savings errors (zero ground-truth denominator) are
recorded as null with an explicit reason, never via epsilon substitution,
and never counted as passing the <3% target.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
METRICS_PATH = REPO / "results" / "metrics.json"


def _strict_loads(text: str):
    def _reject(constant: str):
        raise ValueError(f"non-standard JSON constant: {constant}")

    return json.loads(text, parse_constant=_reject)


class TestMetricsJsonStrict:
    """Committed metrics file must parse under a strict JSON parser."""

    def test_metrics_json_strict_standard(self):
        text = METRICS_PATH.read_text()
        data = _strict_loads(text)  # raises on NaN/Infinity tokens
        assert "week6c_stageA" in data

    def test_undefined_relative_errors_are_null_with_reason(self):
        data = _strict_loads(METRICS_PATH.read_text())
        profiles = data["week6c_stageA"]["profiles"]
        assert set(profiles) == {"steady", "cyclic", "bursty"}
        for name, prof in profiles.items():
            pct = prof["estimation_error_pct"]
            if pct is None:
                assert prof["estimation_error_undefined_reason"], name
                assert "zero" in prof["estimation_error_undefined_reason"].lower(), name
                assert prof["rel_error_meaningful"] is False, name
            else:
                assert isinstance(pct, (int, float)), name
                assert prof["rel_error_meaningful"] is True, name
            # Ground truth, estimate, and both absolute errors always present.
            for key in (
                "ground_truth_savings_kwh",
                "estimated_savings_kwh",
                "signed_error_kwh",
                "abs_error_kwh",
                "rel_error_denominator_kwh",
            ):
                assert isinstance(prof[key], (int, float)), (name, key)
        summary = data["week6c_stageA"]["summary"]
        if summary["max_abs_est_error_pct"] is None:
            assert summary["max_abs_est_error_undefined_reason"], "summary"
            assert summary["n_finite_est_errors"] == 0, "summary"


class TestEstimationErrorFields:
    """Unit contract for sim.run_week6.estimation_error_fields."""

    def test_finite_case_unchanged(self):
        from sim.run_week6 import estimation_error_fields

        pct, meaningful, reason = estimation_error_fields(-0.001, -0.00102)
        assert pct == pytest.approx(2.0)
        assert meaningful is True
        assert reason is None

    def test_zero_denominator_is_null_not_epsilon(self):
        from sim.run_week6 import estimation_error_fields

        pct, meaningful, reason = estimation_error_fields(0.0, 0.000642)
        assert pct is None
        assert meaningful is False
        assert reason and "zero" in reason.lower()
        # Payload with the triple must serialize under a strict encoder.
        payload = {
            "estimation_error_pct": pct,
            "estimation_error_undefined_reason": reason,
            "signed_error_kwh": 0.000642,
            "abs_error_kwh": 0.000642,
        }
        json.dumps(payload, allow_nan=False)

    def test_both_zero_is_null(self):
        from sim.run_week6 import estimation_error_fields

        pct, meaningful, reason = estimation_error_fields(0.0, 0.0)
        assert pct is None
        assert meaningful is False
        assert reason
