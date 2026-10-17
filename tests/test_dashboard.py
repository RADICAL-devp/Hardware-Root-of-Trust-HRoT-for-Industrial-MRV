"""Week 8 tests: dashboard loads metrics and renders without errors."""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_dashboard_module_imports():
    import dashboard.app  # noqa: F401


def test_dashboard_loads_metrics():
    import dashboard.app as app

    m = app.load_metrics()
    assert "week6m_coverage" in m
    assert "week7_attacks" in m


def test_dashboard_metrics_strict_json():
    import json as _json

    _json.loads((REPO / "results" / "metrics.json").read_text())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
