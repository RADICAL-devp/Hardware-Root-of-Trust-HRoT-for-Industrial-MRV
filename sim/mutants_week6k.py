"""Week 6K mutation check: does the test suite actually catch broken physics?

Six mutants are applied one at a time to ``plant/dq_machine.py`` (source text
rewrite), ``pytest -x`` is run on ``tests/test_week6k_dq.py``, the FIRST
failing test is recorded, and the module is restored.

    uv run python sim/mutants_week6k.py

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
TEST_FILE = "tests/test_week6k_dq.py"

MUTANTS = {
    "M-6K-1 torque sign flipped": [
        (
            "torque = 1.5 * pole_pairs(p) * (lmq * i_dr - lmd * i_qr)",
            "torque = 1.5 * pole_pairs(p) * (lmd * i_qr - lmq * i_dr)",
        )
    ],
    "M-6K-2 L_m -> L_lr in node KCL": [
        (
            "k = 1.0 / li.l_ls + 1.0 / li.l_lr + 1.0 / li.l_m",
            "k = 1.0 / li.l_ls + 1.0 / li.l_lr + 1.0 / li.l_lr",
        )
    ],
    "M-6K-3 stator omega_e cross-term sign": [
        (
            "a.v_ds - p.r1_Ohm * a.i_ds + w_e * x4[1]",
            "a.v_ds - p.r1_Ohm * a.i_ds - w_e * x4[1]",
        )
    ],
    "M-6K-4 no sub-stepping (one RK4 step per output sample)": [
        ("h = dt / n_sub", "h = dt"),
        ("for j in range(n_sub):", "for j in range(1):"),
    ],
    "M-6K-5 re-imposed 0.3 N*m 100 Hz torque ripple": [
        (
            'out["torque"][k] = a.torque_Nm',
            'out["torque"][k] = a.torque_Nm + 0.3 * np.sin(2.0 * np.pi * 100.0 * tk)',
        )
    ],
    "M-6K-6 shunt R_c removed (1e12)": [("rc_Ohm: float = 350.0", "rc_Ohm: float = 1e12")],
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
            [
                "uv",
                "run",
                "pytest",
                TEST_FILE,
                "-x",
                "-q",
                "--no-header",
            ],
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
    print("\n=== Week 6K mutant first catchers ===")
    for name, first, tail in rows:
        print(f"{name:56s} -> {first}   [{tail}]")
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
