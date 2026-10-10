"""Week 5d4: UP5K fit probe record (slow: runs yosys + nextpnr-ice40).

Reproduces ``synth/gen_fit_wrapper.py`` (two placements: as-written and
logic-only wrapper) and pins the utilisation record in
``results/synth_fit.json`` — values come from the actual run, never
invented. Requires ``nextpnr-ice40`` (``brew install nextpnr-ice40``);
skips with an explicit message when absent.

Current honest verdict (measured): ICESTORM_LC 23,602/5,280 (447%) as
written, 23,936/5,280 (453%) with the bench bus XOR-reduced — the core
does NOT fit a UP5K as coded. Fmax is therefore not measurable until it
fits. If the design later shrinks below capacity this test must be
re-pinned, not papered over.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIT = REPO / "results" / "synth_fit.json"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def fit_metrics() -> dict:
    if shutil.which("nextpnr-ice40") is None:
        pytest.skip("nextpnr-ice40 missing (brew install nextpnr-ice40)")
    run = subprocess.run(
        ["uv", "run", "python", str(REPO / "synth" / "gen_fit_wrapper.py")],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert run.returncode == 0, run.stderr[-500:]
    return json.loads(FIT.read_text())


def test_fit_probe_records_placement_attempts(fit_metrics):
    for tag in ("as_written", "logic_only"):
        rec = fit_metrics[tag]
        assert rec["rc"] != 0, f"{tag}: placement unexpectedly succeeded"
        assert not rec["placed"]
        lc = rec["icestorm_lc"]
        assert lc and lc["capacity"] == 5280, f"{tag}: {lc}"


def test_fit_probe_reports_core_capacity_exceeded(fit_metrics):
    """Honest: the attested core as coded exceeds a single UP5K (~4.5x)."""
    for tag in ("as_written", "logic_only"):
        lc = fit_metrics[tag]["icestorm_lc"]["used"]
        assert lc > 5280, f"{tag}: now fits ({lc} LCs) — re-pin this test"
    lc_lo = fit_metrics["logic_only"]["icestorm_lc"]["used"]
    assert lc_lo > fit_metrics["as_written"]["icestorm_lc"]["used"] * 0.9
