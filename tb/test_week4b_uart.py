"""Week 4b uart_rx cocotb under Verilator + Icarus (via cocotb-test).

Always runs with waves (cheap: thousands of cycles); the VCD stays in
sim_build unless a failure occurs, in which case it is copied to results/.
"""

import os
import shutil
from pathlib import Path

from cocotb_test.simulator import run

from tb.sim_retry import flaky_banner, is_infra_crash

REPO = Path(__file__).resolve().parents[1]
TOP = "uart_rx"
MOD = "tb.w4b_uart_cocotb"


def _run(sim: str, waves: bool) -> None:
    os.environ["WEEK4B_TAG"] = sim
    case = os.environ.get("WEEK4B_CASE") or None  # e.g. test_uart_baud_tolerance
    # Waveform policy (Week 4b2 root cause): every Verilator build is
    # trace-capable (waves=True always — all Mdirs share one
    # sim_build/verilator.o, which must link against trace-enabled model
    # objects), while --trace (the actual dump) is passed only when
    # capturing. Icarus needs no defines and dumps by default under waves.
    vtrace = sim == "verilator"
    plus = ["--trace"] if (vtrace and waves) else []
    cflags = ["-CFLAGS", "-DVM_TRACE=1", "-CFLAGS", "-DVM_TRACE_FST=1"] if vtrace else []
    mk = ["OPT_FAST=-DVM_TRACE=1 -DVM_TRACE_FST=1"] if vtrace else []
    run(
        verilog_sources=[str(REPO / "rtl" / "uart_rx.v")],
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
        sim_build=str(REPO / "sim_build" / f"week4b_{TOP}_{sim}"),
    )


def _save_vcd(sim: str) -> str:
    # Icarus writes <toplevel>.fst into the build dir; Verilator writes
    # dump.fst to the run CWD (repo root). Take the newest of either.
    build = REPO / "sim_build" / f"week4b_{TOP}_{sim}"
    cands = [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")]
    cands += [p for p in (REPO / "dump.fst", REPO / "dump.vcd") if p.exists()]
    dumps = sorted(cands, key=lambda p: p.stat().st_mtime)
    assert dumps, f"no waveform found under {build} or repo root"
    dest = REPO / "results" / f"week4b_{TOP}_{sim}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


def test_uart_via_verilator_and_icarus():
    for sim in ("verilator", "icarus"):
        try:
            _run(sim, waves=True)
        except BaseException as e:
            if is_infra_crash(e):
                # Dead simulator renders no verdict: one retry, loudly bannered.
                print(f"FLAKY-INFRA {TOP} [{sim}]: {e!r}; retrying once")
                try:
                    _run(sim, waves=True)
                except BaseException as e2:
                    print(f"FLAKY-INFRA {TOP} [{sim}] retry failed: {e2!r}")
                else:
                    print(flaky_banner(TOP, sim, e))
                    continue
            vcd = _save_vcd(sim)
            raise AssertionError(f"{TOP} [{sim}] FAILED; VCD saved to {vcd}")
