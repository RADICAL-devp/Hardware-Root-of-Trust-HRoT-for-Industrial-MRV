"""Week 5a frame_rx cocotb under Verilator + Icarus (via cocotb-test).

Follows the Week 4b wrapper pattern: every Verilator build is
trace-capable; on failure the waveform is copied to results/.
"""

import os
import shutil
from pathlib import Path

from cocotb_test.simulator import run

REPO = Path(__file__).resolve().parents[1]
TOP = "frame_rx"
MOD = "tb.w5_frame_cocotb"


def _run(sim: str, waves: bool) -> None:
    os.environ["WEEK4B_TAG"] = sim
    case = os.environ.get("WEEK4B_CASE") or None
    vtrace = sim == "verilator"
    plus = ["--trace"] if (vtrace and waves) else []
    cflags = ["-CFLAGS", "-DVM_TRACE=1", "-CFLAGS", "-DVM_TRACE_FST=1"] if vtrace else []
    mk = ["OPT_FAST=-DVM_TRACE=1 -DVM_TRACE_FST=1"] if vtrace else []
    run(
        verilog_sources=[str(REPO / "rtl" / "frame_rx.v")],
        toplevel=TOP,
        module=MOD,
        simulator=sim,
        testcase=case,
        plus_args=plus,
        verilog_compile_args=cflags,
        make_args=mk,
        toplevel_lang="verilog",
        python_search=[str(REPO)],
        includes=[str(REPO / "rtl")],
        waves=True if vtrace else waves,
        sim_build=str(REPO / "sim_build" / f"week5a_{TOP}_{sim}"),
    )


def _save_vcd(sim: str) -> str:
    build = REPO / "sim_build" / f"week5a_{TOP}_{sim}"
    cands = [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")]
    cands += [p for p in (REPO / "dump.fst", REPO / "dump.vcd") if p.exists()]
    dumps = sorted(cands, key=lambda p: p.stat().st_mtime)
    assert dumps, f"no waveform found under {build} or repo root"
    dest = REPO / "results" / f"week5a_{TOP}_{sim}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


def test_frame_rx_via_verilator_and_icarus():
    for sim in ("verilator", "icarus"):
        try:
            _run(sim, waves=bool(int(os.environ.get("WAVES", 0))))
        except BaseException as e:
            # Print the root error before the waves rerun swallows it.
            print(f"week5a {TOP} [{sim}] first attempt failed: {e!r}")
            try:
                _run(sim, waves=True)
            except BaseException as e2:
                print(f"week5a {TOP} [{sim}] waves rerun failed: {e2!r}")
            vcd = _save_vcd(sim)
            raise AssertionError(f"{TOP} [{sim}] FAILED; VCD saved to {vcd}")
