"""Week 5d4: UP5K fit probe for the attested core (reproducible).

Runs TWO placements and records both utilisation reports to
``results/synth_fit.json`` (committed, values from the actual run):

1. ``as-written``: ``nextpnr-ice40 --up5k --package sg48`` on the
   flattened top produced by ``synth/top.ys`` (42 ports, 300+ bit bench
   bus) — the "as coded" number.
2. ``logic-only``: same P&R on ``synth_out/top_fit``, a generated
   wrapper that XOR-reduces every top-level output into a single
   ``probe_out`` pin (keeping all consumer logic alive, using ~6 pins).
   This separates "core too big" from "bench bus too big". It is a FIT
   PROBE only, NOT a chip design.

nextpnr-ice40 is present via ``brew install nextpnr-ice40``. If the
placer overflows, rc != 0 and ``ICESTORM_LC use/capacity`` still
reports the utilisation; we record rc and the numbers honestly.

Usage: uv run python synth/gen_fit_wrapper.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "synth_out"
TOP_JSON = REPO / "results" / "synth_top.json"
FIT_JSON = REPO / "results" / "synth_fit.json"

SOURCES = [
    "rtl/top.v",
    "rtl/uart_rx.v",
    "rtl/frame_rx.v",
    "rtl/zc_detect.v",
    "rtl/power_calc.v",
    "rtl/div_fsm.v",
    "rtl/sqrt_fsm.v",
    "rtl/record_agg.v",
    "rtl/chain_ctrl.v",
    "rtl/win_counter.v",
    "rtl/hmac_core.v",
    "rtl/sha256_wrap.v",
    "rtl/skid_buf.v",
    "rtl/third_party/secworks-sha256/src/rtl/sha256_core.v",
    "rtl/third_party/secworks-sha256/src/rtl/sha256_w_mem.v",
    "rtl/third_party/secworks-sha256/src/rtl/sha256_k_constants.v",
]

UTIL_RE = re.compile(r"ICESTORM_LC:\s+(\d+)/\s*(\d+)\s+(\d+)%|SB_IO:\s+(\d+)/\s*(\d+)\s+(\d+)%")


def _parse_util(log: str) -> dict:
    lc = io = None
    for m in re.finditer(r"(ICESTORM_LC|SB_IO):\s+(\d+)/\s*(\d+)\s+(\d+)%", log):
        if m.group(1) == "ICESTORM_LC":
            lc = {"used": int(m.group(2)), "capacity": int(m.group(3)), "pct": int(m.group(4))}
        else:
            io = {"used": int(m.group(2)), "capacity": int(m.group(3)), "pct": int(m.group(4))}
    return {"icestorm_lc": lc, "sb_io": io}


def _nextpnr(name: str, json_in: Path, asc: Path, tag: str) -> dict:
    log = OUT / f"fit_{tag}_nextpnr.log"
    run = subprocess.run(
        [
            "nextpnr-ice40",
            "--up5k",
            "--package",
            "sg48",
            "--json",
            str(json_in),
            "--asc",
            str(asc),
            "--freq",
            "12",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    log.write_text(run.stdout + run.stderr)
    combined = run.stdout + run.stderr
    return {"rc": run.returncode, "placed": run.returncode == 0, **_parse_util(combined)}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    records = {}
    records["as_written"] = _nextpnr("as_written", TOP_JSON, OUT / "top.asc", "as_written")

    design = json.loads(TOP_JSON.read_text())
    flat, _ = max(
        ((n, len(m.get("cells", {}))) for n, m in design["modules"].items()),
        key=lambda t: t[1],
    )
    outputs = [n for n, s in design["modules"][flat]["ports"].items() if s["direction"] == "output"]
    wires, conns, red = [], [], []
    for name in outputs:
        w = len(design["modules"][flat]["ports"][name]["bits"])
        wires.append(f"  wire [{w - 1}:0] o_{name};")
        conns.append(f"    .{name}(o_{name}),")
        red.append(f"o_{name}")
    (OUT / "top_fit.v").write_text(
        "module top_fit (clk, rst_n, rx, hmac_key_data_in, hmac_key_valid_in, probe_out);\n"
        "  input clk, rst_n, rx;\n"
        "  input [255:0] hmac_key_data_in;\n"
        "  input hmac_key_valid_in;\n"
        "  output probe_out;\n"
        + "\n".join(wires)
        + "\n  top u_top (\n"
        + "    .clk(clk), .rst_n(rst_n), .rx(rx),\n"
        + "    .hmac_key_data_in(hmac_key_data_in), .hmac_key_valid_in(hmac_key_valid_in),\n"
        + "\n".join(conns)
        + "\n  );\n"
        + "  assign probe_out = ^({"
        + ", ".join(red)
        + "});\n"
        + "endmodule\n"
    )
    srclist = " ".join(SOURCES)
    (OUT / "fit.ys").write_text(
        f"read_verilog {srclist}\n"
        f"read_verilog {OUT / 'top_fit.v'}\n"
        "hierarchy -top top_fit\n"
        "synth_ice40 -top top_fit\n"
        f"write_json {OUT / 'top_fit.json'}\n"
        "stat\n"
    )
    run = subprocess.run(
        ["yosys", str(OUT / "fit.ys")],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    (OUT / "fit_synth.log").write_text(run.stdout + run.stderr)
    if run.returncode != 0:
        raise SystemExit(f"fit synth failed rc={run.returncode}")
    records["logic_only"] = _nextpnr(
        "logic_only", OUT / "top_fit.json", OUT / "top_fit.asc", "logic_only"
    )

    fit = {
        "tool": "nextpnr-ice40",
        "device": "UP5K",
        "package": "sg48",
        "freq_target_mhz": 12,
        "note": "fit probe logs in synth_out/ (gitignored); ICESTORM_LC 5280-cap device; "
        "placement likely overflows as written — recorded honestly, not fixed by silence.",
        **records,
    }
    FIT_JSON.write_text(json.dumps(fit, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FIT_JSON.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
