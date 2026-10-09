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

`is_infra_crash` matches ONLY unmistakable no-verdict signatures (dead or
stillborn simulators, never assertion text). When in doubt it returns
False (strict).

Week5d1b extension (root-caused, not loosened): two STARTUP-death
signatures join the process-death list, because a sim that never ran
its test body renders no verdict exactly like a segfaulted one:
- empty results XML (`ParseError: no element found`): cocotb-test found
  zero test results, i.e. the sim died during startup imports;
- the frozen-dataclass interpreter pathology (`cannot access free
  variable '__dataclass_builtins_object__'`): CPython failing to exec a
  frozen `__init__`, observed at sim startup (including inside
  third-party `pytest`, which cocotb itself imports) and once at
  runtime. A deterministic test bug can NEVER produce this literal
  (genuine typos read `NameError: name 'X' is not defined`, which still
  returns False — pinned in tests/test_sim_retry.py).
A deterministic failure still fails twice (retry included) and the node
fails hard; only a clean rerun after a no-verdict event passes, loudly.
"""

import re

_CRASH_PATTERNS = (
    r"terminated with error -\d+",  # cocotb-test: sim subprocess died by signal
    r"SIGSEGV",
    r"SIGABRT",
    r"Segmentation fault",
    r"no element found: line 1, column 0",  # empty results: stillborn sim
    r"cannot access free variable '__dataclass_builtins_object__'",  # noqa: E501 (literal)
)


def is_infra_crash(exc: BaseException) -> bool:
    """True iff `exc` is a simulator process death (no verdict rendered)."""
    text = f"{type(exc).__name__}: {exc!r}"
    return any(re.search(p, text) for p in _CRASH_PATTERNS)


def flaky_banner(case: str, sim: str, exc: BaseException) -> str:
    """Single-line banner for logs and CI warning annotations."""
    return f"FLAKY-INFRA-PASS {case} [{sim}]: first attempt died ({exc!r}); rerun clean"
