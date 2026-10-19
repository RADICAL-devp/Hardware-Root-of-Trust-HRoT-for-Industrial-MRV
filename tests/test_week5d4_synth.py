"""Week 5d4: synthesis resource validation for the attested core (Yosys).

Runs the canonical ``synth/top.ys`` flow once (module-scoped fixture;
the ABC mapping takes ~2 min), then asserts:
- Yosys elaborates and ``check`` reports no problems (the driver exits 0).
- ZERO inferred latches (``select -count t:$dlatch`` = 0, asserted again
  inside the flow via ``select -assert-none``).
- Elaborated parameters are the production defaults and there are NO
  test-hook overrides (B3): DEVICE_ID=1, SKID_DEPTH=16,
  LUT_MEAN_ENABLE=0, CLK_PER_BIT=8, every *_CNT_INIT elaborated == 0.
- Pinned resource counts (regen: run ``uv run python synth/run_synth.py``
  once and copy the values — same as metrics.json discipline).
- synth scripts set no parameters (static), plus a behavioral negative
  control proving a ``chparam`` really does change the elaborated dump
  the guard protects.

Honest-scope note: these numbers describe the RTL as written today;
they do NOT claim fitting or timing, which are separate (nextpnr/icetime)
deliverables.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SYNTH = REPO / "synth"
DRIVER = SYNTH / "run_synth.py"
METRICS = REPO / "results" / "synth_metrics.json"

PINNED_RESOURCES = {
    "lut4": 15851,
    "ff_total": 10979,
    "carry": 2768,
    "bram_4k": 0,
    "latches_inferred": 0,
}

FORBIDDEN_HOOKS = re.compile(r"\b(FERR_CNT_INIT|OVERRUN_CNT_INIT|CNT_INIT)\b")
PARAM_SET = re.compile(r"-chparam\b|(?<!\w)-P\b|-G\w|\bchparam\b")


def _strip_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


@pytest.fixture(scope="module")
def synth_metrics() -> dict:
    if not METRICS.exists() or not SYNTH.glob("*.ys"):
        pytest.skip("synth flow files missing")
    yosys = shutil.which("yosys")
    if yosys is None:
        pytest.skip("yosys not available on this platform (CI) - see AGENTS.md")
    # Only run the Yosys-dependent checks when the installed Yosys matches
    # the pinned version the committed metrics were generated with; a
    # different Yosys version changes resource counts and would false-fail.
    try:
        ver = subprocess.run(
            [yosys, "-V"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except Exception:
        ver = ""
    pinned = json.loads(METRICS.read_text()).get("tool_version", "")
    if pinned and ver and ver != pinned:
        pytest.skip(f"yosys version mismatch (have {ver!r}, pinned {pinned!r})")
    run = subprocess.run(
        ["uv", "run", "python", str(DRIVER)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert run.returncode == 0, f"synth driver failed:\n{run.stdout}\n{run.stderr}"
    return json.loads(METRICS.read_text())


def test_synth_driver_runs_clean(synth_metrics):
    assert synth_metrics["dut"] == "top"
    assert synth_metrics["flat_module"] == "top"  # synth_ice40 flattens into top
    assert "yosys" in synth_metrics["tool_version"].lower()


def test_no_inferred_latches(synth_metrics):
    assert synth_metrics["latches_inferred"] == 0
    assert synth_metrics["positive_latch_messages"] == []


def test_elaborated_params_are_production_defaults(synth_metrics):
    p = synth_metrics["params"]
    assert p.get("top:DEVICE_ID") == 1, "synth must elaborate the default device id"
    assert p.get("top:SKID_DEPTH") == 16, "synth must elaborate the default skid depth"
    assert p.get("power_calc:LUT_MEAN_ENABLE") == 0
    assert p.get("uart_rx:CLK_PER_BIT") == 8
    # Any *_CNT_INIT that exists was declared with its default 0 (B3) —
    # an override would mean a test hook reached synthesis.
    for k, v in p.items():
        if FORBIDDEN_HOOKS.search(k):
            assert v == 0, f"test-hook override elaborated in synthesis: {k}={v}"


def test_resource_counts_pinned(synth_metrics):
    for k, expected in PINNED_RESOURCES.items():
        assert synth_metrics[k] == expected, (
            f"resource {k} = {synth_metrics[k]}, pinned {expected} "
            "(regen: uv run python synth/run_synth.py, then update PINNED_RESOURCES)"
        )


def test_synth_scripts_set_no_parameter_overrides():
    ys_files = sorted(SYNTH.glob("*.ys"))
    assert ys_files, "no synth/*.ys to guard"
    for ys in ys_files:
        hits = [pat.pattern for pat in PARAM_SET.finditer(_strip_comments(ys.read_text()))]
        assert not hits, f"parameter setting in {ys.name}: {hits}"


def test_parameter_guard_negative_control(tmp_path):
    """The guard is not vacuous: effective parameter values ARE in the
    elaborated dump (so an override would show up), and the static scanner
    rejects every command form that synth scripts use to force them."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys not available on this platform (CI)")
    leaf = tmp_path / "leaf.v"
    leaf.write_text("module m #(parameter P = 1) (); endmodule\n")

    def elab(inst_src: str) -> int:
        top = tmp_path / "top.v"
        top.write_text(f"module top; {inst_src} endmodule\n")
        ys = tmp_path / "top.ys"
        ys.write_text(
            f"read_verilog {leaf}\nread_verilog {top}\nhierarchy -top top\n"
            f"write_json {tmp_path}/out.json\n"
        )
        run = subprocess.run(["yosys", str(ys)], capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr[-400:]
        raw = json.loads((tmp_path / "out.json").read_text())["modules"]["top"]["cells"]["u1"][
            "parameters"
        ]["P"]
        return int(raw, 2)

    base = elab("m #(.P(1)) u1();")  # production default
    overridden = elab("m #(.P(7)) u1();")  # what a test hook would force
    assert base == 1
    assert overridden == 7  # effective values are observable in the dump
    # The static guard catches the equivalent command-line options:
    for evil in (
        "hierarchy -top top; chparam -set P 7 top",
        "hierarchy -top top; -GP=7 m",
        "read_verilog -P 7 m; hierarchy -top top",
    ):
        assert PARAM_SET.search(_strip_comments(evil)) is not None, evil
