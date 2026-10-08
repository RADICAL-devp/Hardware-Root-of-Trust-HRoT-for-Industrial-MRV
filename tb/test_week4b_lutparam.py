"""Week 4b2 power_calc exact-only build (LUT_MEAN_ENABLE=0), both sims."""

import os
from pathlib import Path

from cocotb_test.simulator import run

REPO = Path(__file__).resolve().parents[1]
TOP = "power_calc"
MOD = "tb.w4b_lutparam_cocotb"


def _run(sim: str, waves: bool) -> None:
    # Waveform policy (Week 4b2 root cause): every Verilator build is
    # trace-capable (waves=True always — shared sim_build/verilator.o must
    # link against trace-enabled model objects); --trace only when capturing.
    vtrace = sim == "verilator"
    plus = ["--trace"] if (vtrace and waves) else []
    cflags = ["-CFLAGS", "-DVM_TRACE=1", "-CFLAGS", "-DVM_TRACE_FST=1"] if vtrace else []
    mk = ["OPT_FAST=-DVM_TRACE=1 -DVM_TRACE_FST=1"] if vtrace else []
    run(
        verilog_sources=[
            str(REPO / "rtl" / s) for s in ("power_calc.v", "div_fsm.v", "sqrt_fsm.v")
        ],
        toplevel=TOP,
        module=MOD,
        simulator=sim,
        plus_args=plus,
        verilog_compile_args=cflags,
        make_args=mk,
        toplevel_lang="verilog",
        python_search=[str(REPO)],
        includes=[str(REPO / "rtl")],
        parameters={"LUT_MEAN_ENABLE": 0},
        waves=True if vtrace else waves,
        sim_build=str(REPO / "sim_build" / f"week4b_{TOP}_nolut_{sim}"),
    )


def test_lutparam_via_verilator_and_icarus():
    for sim in ("verilator", "icarus"):
        _run(sim, waves=bool(int(os.environ.get("WAVES", 0))))
