"""Week 5d4: deterministic Yosys driver for the attested core.

Runs ``synth/top.ys`` from the repo root, parses the ``write_json``
design dump and the stat/latch lines, and writes
``results/synth_metrics.json`` (committed, deterministic for a pinned
Yosys version) plus ``results/synth_top.json`` / ``results/synth_run.log``
(generated, gitignored). Exit non-zero if Yosys fails.

Resource contract (reported honestly, never invented):
- LUT/FF/CARRY/BRAM counts come from the actual Yosys run below.
- Latches counted with ``select -count t:$dlatch`` right after ``proc``;
  asserted 0 by tests/test_week5d4_synth.py.
- Elaborated params assert production defaults (DEVICE_ID=1,
  SKID_DEPTH=16, power_calc.LUT_MEAN_ENABLE=0, uart_rx.CLK_PER_BIT=8)
  and that no test-hook (\\*_CNT_INIT) parameter got elaborated.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
YS = REPO / "synth" / "top.ys"
TOP_JSON = REPO / "results" / "synth_top.json"
ELAB_JSON = REPO / "results" / "synth_elab.json"
METRICS_JSON = REPO / "results" / "synth_metrics.json"
LOG = REPO / "results" / "synth_run.log"

TEST_HOOK_PATTERN = re.compile(r"\b(FERR_CNT_INIT|OVERRUN_CNT_INIT|CNT_INIT)\b")
PARAM_SET_PATTERN = re.compile(r"-chparam\b|(?<!\w)-P\b|-G\w|\bchparam\b")
POSITIVE_LATCH = re.compile(r"Latch inferred for signal")

SB_FF_TYPES = ("SB_DFFER", "SB_DFFR", "SB_DFFS", "SB_DFFES", "SB_DFFSS")

# resource cell types we report (anything else lands in other_cells)
_RESOURCE_TYPES = {
    "SB_LUT4",
    "SB_DFFER",
    "SB_DFFR",
    "SB_DFFS",
    "SB_DFFES",
    "SB_DFFSS",
    "SB_CARRY",
}


def _strip_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _yosys_version() -> str:
    out = subprocess.run(["yosys", "-V"], capture_output=True, text=True, check=False, cwd=REPO)
    return (out.stdout or out.stderr).strip().splitlines()[0]


def _count_by_type(cells: dict) -> dict:
    out = {}
    for c in cells.values():
        t = c.get("type", "?")
        out[t] = out.get(t, 0) + 1
    return out


def _stats_from(design: dict) -> dict:
    """Cell counts on the flattened top module (synth_ice40 flatten)."""
    mods = design.get("modules", {})
    name, cells = max(((n, m.get("cells", {})) for n, m in mods.items()), key=lambda t: len(t[1]))
    by_type = _count_by_type(cells)
    ff_by_type = {t: n for t, n in by_type.items() if t in SB_FF_TYPES}
    return {
        "flat_module": name,
        "lut4": by_type.get("SB_LUT4", 0),
        "ff_total": sum(ff_by_type.values()),
        "ff_by_type": ff_by_type,
        "carry": by_type.get("SB_CARRY", 0),
        "bram_4k": sum(1 for t in by_type if t.startswith("SB_RAM40")),
        "other_cells": {t: n for t, n in by_type.items() if t not in _RESOURCE_TYPES},
    }


def _paramod_base(cell_type: str) -> str:
    """Paramod name -> module: ``$paramod\\power_calc\\...`` -> ``power_calc``."""
    return cell_type.replace("$paramod\\", "", 1).split("\\", 1)[0]


def _params_from(design: dict) -> dict:
    """Effective parameter values from the PRE-techmap elaboration dump.

    top carries DEVICE_ID/SKID_DEPTH defaults; each $paramod module carries
    the specialized value of every parameter (names encode the value in the
    module name; the module's ``parameter_default_values`` hold actuals).
    """
    mods = design.get("modules", {})
    out = {}
    for mname, m in mods.items():
        pvs = {k: _norm_param(v) for k, v in m.get("parameter_default_values", {}).items()}
        if mname == "top":
            for k, v in pvs.items():
                out[f"top:{k}"] = v
        elif "$paramod" in mname:
            base = _paramod_base(mname)
            for k, v in pvs.items():
                out[f"{base}:{k}"] = v
    return out


def _norm_param(value):
    """Yosys json params are ints, binary digit strings, or names.

    Numeric vectors arrive as width-padded binary strings (e.g.
    ``DEVICE_ID -> '0000...0001'``, ``SKID_DEPTH -> '...0010000'``); parse
    those as base-2. Everything else (IO_STANDARD names, 0bxx literals) is
    kept verbatim for equality checks.
    """
    raw = value.get("value") if isinstance(value, dict) else value
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw and set(raw) <= {"0", "1"}:
        return int(raw, 2)
    return raw


def main() -> int:
    log = LOG
    log.parent.mkdir(exist_ok=True)
    ys = _strip_comments(YS.read_text())
    if TEST_HOOK_PATTERN.search(ys) or PARAM_SET_PATTERN.search(ys):
        raise SystemExit("synth/top.ys sets a parameter — guard violated")
    proc = subprocess.run(["yosys", str(YS)], capture_output=True, text=True, cwd=REPO, check=False)
    logtext = proc.stdout + proc.stderr
    log.write_text(logtext)
    if proc.returncode != 0:
        raise SystemExit(f"yosys failed rc={proc.returncode}: {logtext[-800:]}")

    if not TOP_JSON.exists() or not ELAB_JSON.exists():
        raise SystemExit(f"synth produced no {TOP_JSON.name}/{ELAB_JSON.name}")
    design = json.loads(TOP_JSON.read_text())
    stats = _stats_from(design)

    m = re.findall(r"Selected (\d+) objects", logtext)
    # select -count prints nothing for an empty selection (0 latches);
    # select -assert-none t:$dlatch above already hard-fails on any latch.
    dlatches = int(m[-1]) if m else 0
    positive = [ln for ln in logtext.splitlines() if POSITIVE_LATCH.search(ln)]

    params = _params_from(json.loads(ELAB_JSON.read_text()))

    metrics = {
        "tool": "yosys",
        "tool_version": _yosys_version(),
        "flow": str(YS.relative_to(REPO)),
        "dut": "top",
        "latches_inferred": dlatches,
        "positive_latch_messages": positive,
        **stats,
        "params": params,
    }
    METRICS_JSON.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(f"wrote {METRICS_JSON.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
