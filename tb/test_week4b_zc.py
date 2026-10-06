"""Week 4b zc_detect cocotb under Verilator + Icarus (via cocotb-test).

On failure the failing simulator reruns with waves and its VCD is copied
to results/; bounds are never loosened (the rerun must fail identically).
"""

import os
import shutil
from pathlib import Path

from cocotb_test.simulator import run

REPO = Path(__file__).resolve().parents[1]
TOP = "zc_detect"
MOD = "tb.w4b_zc_cocotb"


def _run(sim: str, waves: bool) -> None:
    os.environ["WEEK4B_TAG"] = sim
    run(
        verilog_sources=[str(REPO / "rtl" / "zc_detect.v")],
        toplevel=TOP,
        module=MOD,
        simulator=sim,
        toplevel_lang="verilog",
        python_search=[str(REPO)],
        includes=[str(REPO / "rtl")],
        waves=waves,
        sim_build=str(REPO / "sim_build" / f"week4b_{TOP}_{sim}"),
    )


def _save_vcd(sim: str) -> str:
    build = REPO / "sim_build" / f"week4b_{TOP}_{sim}"
    dumps = sorted(
        [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")],
        key=lambda p: p.stat().st_mtime,
    )
    assert dumps, f"no waveform found under {build}"
    dest = REPO / "results" / f"week4b_{TOP}_{sim}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


def test_zc_via_verilator_and_icarus():
    for sim in ("verilator", "icarus"):
        try:
            _run(sim, waves=bool(int(os.environ.get("WAVES", 0))))
        except BaseException:
            _run(sim, waves=True)
            vcd = _save_vcd(sim)
            raise AssertionError(f"{TOP} [{sim}] FAILED; VCD saved to {vcd}")
