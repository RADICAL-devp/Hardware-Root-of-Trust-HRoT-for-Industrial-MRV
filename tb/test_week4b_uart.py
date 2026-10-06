"""Week 4b uart_rx cocotb under Verilator + Icarus (via cocotb-test).

Always runs with waves (cheap: thousands of cycles); the VCD stays in
sim_build unless a failure occurs, in which case it is copied to results/.
"""

import os
import shutil
from pathlib import Path

from cocotb_test.simulator import run

REPO = Path(__file__).resolve().parents[1]
TOP = "uart_rx"
MOD = "tb.w4b_uart_cocotb"


def _run(sim: str, waves: bool) -> None:
    os.environ["WEEK4B_TAG"] = sim
    run(
        verilog_sources=[str(REPO / "rtl" / "uart_rx.v")],
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


def test_uart_via_verilator_and_icarus():
    for sim in ("verilator", "icarus"):
        try:
            _run(sim, waves=True)
        except BaseException:
            vcd = _save_vcd(sim)
            raise AssertionError(f"{TOP} [{sim}] FAILED; VCD saved to {vcd}")
