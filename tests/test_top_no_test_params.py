"""Repo guard: top.v and synth scripts carry no test-only parameters (5d2).

Rules (DECISIONS.md B3):
- `FERR_CNT_INIT`, `OVERRUN_CNT_INIT`, `CNT_INIT` must not appear in
  `rtl/top.v` in any form (no declarations, no pass-throughs — the
  production counter and error counters always run defaults).
- `#(...)` overrides in `rtl/top.v` instantiations are allowed ONLY for
  the closed whitelist: `LUT_MEAN_ENABLE(0)`, `DEVICE_ID(x)`,
  `CLK_PER_BIT(8)` (production value per B1), `DEPTH(SKID_DEPTH)`
  (top's parameter pass-through; `SKID_DEPTH` default pinned 16).
  Anything else fails.
- `synth/*.ys` must not set parameters (`-chparam`, `-P`, `-G<param>`).
Deviations fail loudly here instead of silently reaching synthesis.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOP = REPO / "rtl" / "top.v"

FORBIDDEN_TOKENS = ("FERR_CNT_INIT", "OVERRUN_CNT_INIT", "CNT_INIT")

# (module, param): allowed explicit overrides in top.v instantiations.
WHITELIST = {
    ("uart_rx", "CLK_PER_BIT"): ("8",),
    ("power_calc", "LUT_MEAN_ENABLE"): ("0",),
    ("skid_buf", "DEPTH"): ("SKID_DEPTH",),  # top's param pass-through (default 16)
}


def _strip_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_no_test_hooks_in_top():
    src = _strip_comments(TOP.read_text())
    hits = [t for t in FORBIDDEN_TOKENS if re.search(r"\b" + t + r"\b", src)]
    assert not hits, f"test-only parameters in rtl/top.v: {hits}"


def test_top_instantiation_overrides_whitelisted():
    src = _strip_comments(TOP.read_text())
    # Drop the module header (its own `#(parameter ...)` declaration is
    # not an override; a naive `#(...)\w+(` span would swallow the whole
    # port list and misattribute uart_rx's block to module `top`).
    # The header is everything through the first `);`.
    assert "module" in src.split(");", 1)[0], "module header not found"
    body_src = src.split(");", 1)[1]
    # DEVICE_ID may appear as a top-level parameter declaration and as a
    # pass-through value; every OTHER #(...) override must be whitelisted.
    for m in re.finditer(r"#\s*\((.*?)\)\s*\w+\s*\(", body_src, re.S):
        body = m.group(1)
        for pm in re.finditer(r"\.\s*(\w+)\s*\(\s*([^,)]*?)\s*\)", body):
            param, value = pm.group(1), pm.group(2).strip()
            if param == "DEVICE_ID":
                continue
            # Find the enclosing instance's module: the last word before
            # the #(...) block is the module name ("<module> #(...) <inst> (").
            head = body_src[: m.start()]
            mod = re.findall(r"(\w+)\s*$", head)
            module = mod[-1] if mod else "?"
            allowed = WHITELIST.get((module, param), ())
            assert value in allowed, (
                f"non-whitelisted override .{param}({value}) on {module} "
                f"in rtl/top.v (whitelist: LUT_MEAN_ENABLE=0, DEVICE_ID, CLK_PER_BIT=8)"
            )


def test_top_device_id_default_sane():
    src = _strip_comments(TOP.read_text())
    m = re.search(r"parameter\s+(?:\[.*?\]\s*)?DEVICE_ID\s*=\s*([^;,()\s]+)", src)
    assert m, "top.v declares no DEVICE_ID parameter"
    assert m.group(1).strip() in ("32'd1", "32'h1", "1"), f"DEVICE_ID default {m.group(1)!r} != 1"


def test_top_skid_depth_default_sane():
    src = _strip_comments(TOP.read_text())
    m = re.search(r"parameter\s+(?:\[.*?\]\s*)?SKID_DEPTH\s*=\s*([^;,()\s]+)", src)
    assert m, "top.v declares no SKID_DEPTH parameter"
    assert m.group(1).strip() in ("16",), f"SKID_DEPTH default {m.group(1)!r} != 16"


def test_synth_scripts_set_no_parameters():
    synth = REPO / "synth"
    if not synth.is_dir():
        return  # no synth flow yet (5d4 adds it); static top.v checks still apply
    hits = []
    for ys in sorted(synth.glob("*.ys")):
        text = _strip_comments(ys.read_text())
        for pat in (r"-chparam\b", r"(?<!\w)-P\b", r"-G\w"):
            if re.search(pat, text):
                hits.append(f"{ys.name}: {pat}")
    assert not hits, f"parameter settings in synth scripts: {hits}"
