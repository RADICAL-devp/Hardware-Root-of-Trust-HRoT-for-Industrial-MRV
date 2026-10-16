"""Week 6L mutation check: does the 6L suite catch a broken core-loss form?

Five mutants are applied one at a time to ``plant/dq_machine.py`` (source text
rewrite), ``pytest -x`` is run on ``tests/test_week6l_coreloss.py``, the FIRST
failing test is recorded, and the module is restored.

    uv run python sim/mutants_week6l.py

Safety: the original source is held in memory and rewritten in a ``finally``
block, and the file is re-checked at the end. If the process is killed
mid-run the module can be left mutated; recover with
``git checkout plant/dq_machine.py`` (the tool never runs as part of a gate).

This is a model-checking tool. It shows the suite is not vacuous; it says
nothing about a real machine.
"""

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "plant" / "dq_machine.py"
TEST_FILE = "tests/test_week6l_coreloss.py"

MUTANTS = {
    "M-6L-1 no frequency dependence (R_c_eff = R_c)": [
        ("return p.rc_Ohm * u * u / b", "return p.rc_Ohm")
    ],
    "M-6L-2 wrong eddy fraction (c = 0)": [
        ("core_eddy_frac: float = 0.5", "core_eddy_frac: float = 0.0")
    ],
    "M-6L-3 core loss removed (R_c = 1e12)": [("rc_Ohm: float = 350.0", "rc_Ohm: float = 1e12")],
    "M-6L-4 wrong frequency scaling (u not u^2)": [
        ("return p.rc_Ohm * u * u / b", "return p.rc_Ohm * u / b")
    ],
    "M-6L-5 fixed core loss (no flux dependence)": [
        ("p_core = 1.5 * (v_md * i_cd + v_mq * i_cq)", "p_core = 453.429")
    ],
}


def run_one(name: str, patches) -> tuple[str, str, str]:
    original = SRC.read_text()
    text = original
    for old, new in patches:
        if old not in text:
            SRC.write_text(original)
            return name, "PATCH-NOT-FOUND", ""
        text = text.replace(old, new, 1)
    SRC.write_text(text)
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", TEST_FILE, "-x", "-q", "--no-header"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        out = proc.stdout + proc.stderr
        m = re.search(r"FAILED (tests/[^\s]+)", out)
        first = m.group(1) if m else f"(NOT CAUGHT; pytest exit {proc.returncode})"
        tail = re.search(r"(\d+ failed[^\n]*|\d+ passed[^\n]*)", out)
        return name, first, tail.group(1) if tail else ""
    finally:
        SRC.write_text(original)


def main() -> int:
    original = SRC.read_text()
    rows = [run_one(name, patches) for name, patches in MUTANTS.items()]
    print("\n=== Week 6L mutant first catchers ===")
    for name, first, tail in rows:
        print(f"{name:52s} -> {first}   [{tail}]")
    if SRC.read_text() != original:
        print("\nERROR: plant/dq_machine.py was not restored!")
        return 1
    survived = [name for name, first, _ in rows if "NOT CAUGHT" in first]
    if survived:
        print(f"\nSURVIVING MUTANTS: {survived}")
        return 1
    print("\nmodule restored OK; all mutants caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
