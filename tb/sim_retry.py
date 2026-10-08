"""Shared simulator-crash policy for cocotb-test wrappers (Week 5b2).

Background (measured, not assumed): across ~2500 local node-passes plus
one CI run, Verilator subprocesses die nondeterministically a handful of
times (SIGSEGV, `terminated with error -11`) with zero assertion content,
then pass on rerun. A dead simulator renders NO verdict, so retrying it
once cannot hide a real failure: any assertion failure — first attempt
or rerun — still fails the node hard. A crash followed by a clean rerun
passes WITH an explicit FLAKY-INFRA banner (parsed into CI warning
annotations and counted in PROGRESS.md), so infra events stay visible
and rate-tracked instead of silently green.

`is_infra_crash` matches ONLY unmistakable process-death signatures
(signals, not assertion text). When in doubt it returns False (strict).
"""

import re

_CRASH_PATTERNS = (
    r"terminated with error -\d+",  # cocotb-test: sim subprocess died by signal
    r"SIGSEGV",
    r"SIGABRT",
    r"Segmentation fault",
)


def is_infra_crash(exc: BaseException) -> bool:
    """True iff `exc` is a simulator process death (no verdict rendered)."""
    text = f"{type(exc).__name__}: {exc!r}"
    return any(re.search(p, text) for p in _CRASH_PATTERNS)


def flaky_banner(case: str, sim: str, exc: BaseException) -> str:
    """Single-line banner for logs and CI warning annotations."""
    return f"FLAKY-INFRA-PASS {case} [{sim}]: first attempt died ({exc!r}); rerun clean"
