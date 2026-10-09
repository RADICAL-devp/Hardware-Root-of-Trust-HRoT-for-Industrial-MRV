"""Repo guard: no frozen @dataclass in any in-sim-imported module (week5d1b).

Background: two harness flakes root-caused to CPython's frozen-dataclass
machinery (`NameError: cannot access free variable
'__dataclass_builtins_object__'` — once at construction in
`tb/golden.py`, once at import/exec time in a sim process). The fix is
elimination: every module the simulator process imports must construct
no frozen dataclasses. Non-frozen dataclasses are unaffected (plain
`self.x =` init, no builtins closure) and host-only modules are out of
the sim's import closure entirely.

This test walks the in-sim import closure syntactically (entry points:
all `tb/*.py` cocotb/TB modules; following only repo-local `tb/ edge/
sim/ sensors/` imports, including function-level ones since deferred
imports still exec in-sim) and fails on any `@dataclass(frozen=True)`
class definition found. Third-party packages (cocotb, numpy, nacl) are
out of scope — this guards OUR code.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Entry points: every TB/cocotb module that a sim process may load.
# Wrappers (tb/test_*.py) run host-side only and need no scan themselves,
# but scanning them is harmless (they import nothing frozen).
ENTRIES = sorted((REPO / "tb").glob("*.py"))

# Repo-local top-level packages the sim may import (transitively).
LOCAL_TOPS = ("tb", "edge", "sim", "sensors")


def _module_file(dotted: str) -> Path | None:
    """Resolve `pkg.mod` to a repo .py file (package __init__ or module)."""
    parts = dotted.split(".")
    if not parts or parts[0] not in LOCAL_TOPS:
        return None
    base = REPO.joinpath(*parts)
    cand = base.with_suffix(".py")
    if cand.is_file():
        return cand
    init = base / "__init__.py"
    if init.is_file():
        return init
    return None


def _local_imports(path: Path) -> list[str]:
    """Dotted module names imported by `path` (all Import/ImportFrom nodes)."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
    return out


def _frozen_dataclasses(path: Path) -> list[str]:
    """Class names in `path` decorated with frozen=True (@dataclass forms).

    Import-alias aware: `from dataclasses import dataclass as dc` +
    `@dc(frozen=True)` and `import dataclasses as d` + `@d.dataclass(...)`
    are resolved through the file's own import table.
    """
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError):
        return []

    names_dataclass: set[str] = set()  # local names bound to dataclasses.dataclass
    mods_dataclasses: set[str] = set()  # local names bound to the dataclasses module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
            for a in node.names:
                if a.name == "dataclass":
                    names_dataclass.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "dataclasses":
                    mods_dataclasses.add(a.asname or a.name)

    def is_frozen_deco(dec: ast.expr) -> bool:
        if not isinstance(dec, ast.Call):
            return False
        func = dec.func
        is_dc = False
        if isinstance(func, ast.Name) and func.id in names_dataclass:
            is_dc = True
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "dataclass"
            and isinstance(func.value, ast.Name)
            and func.value.id in mods_dataclasses
        ):
            is_dc = True
        if not is_dc:
            return False
        return any(
            kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in dec.keywords
        )

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(is_frozen_deco(d) for d in node.decorator_list):
            found.append(node.name)
    return found


def test_no_frozen_dataclass_in_sim_closure():
    visited: set[Path] = set()
    offenders: dict[str, list[str]] = {}
    stack = list(ENTRIES)
    while stack:
        path = stack.pop()
        if path in visited or not path.is_file():
            continue
        visited.add(path)
        bad = _frozen_dataclasses(path)
        if bad:
            offenders[str(path.relative_to(REPO))] = bad
        for dotted in _local_imports(path):
            target = _module_file(dotted)
            if target is not None and target not in visited:
                stack.append(target)
    assert not offenders, (
        "frozen @dataclass in sim-imported modules (flake mechanism — use NamedTuple): "
        + "; ".join(f"{m}: {', '.join(c)}" for m, c in sorted(offenders.items()))
    )
