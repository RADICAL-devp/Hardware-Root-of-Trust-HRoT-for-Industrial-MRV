"""Week 4b2 record_agg cocotb under Verilator + Icarus (via cocotb-test).

On failure the failing simulator reruns with waves and its VCD is copied
to results/; bounds are never loosened (the rerun must fail identically).
"""

import os
import shutil
from pathlib import Path

from cocotb_test.simulator import run

from tb.sim_retry import flaky_banner, is_infra_crash

REPO = Path(__file__).resolve().parents[1]
TOP = "record_agg"
MOD = "tb.w4b_record_cocotb"


def _run(sim: str, waves: bool) -> None:
    os.environ["WEEK4B_TAG"] = sim
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
        verilog_sources=[str(REPO / "rtl" / "record_agg.v")],
        toplevel=TOP,
        module=MOD,
        simulator=sim,
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
    # Verilator writes dump.fst into the build dir; Icarus writes
    # <toplevel>.fst there too (repo-root dump.* as fallback).
    build = REPO / "sim_build" / f"week4b_{TOP}_{sim}"
    cands = [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")]
    cands += [p for p in (REPO / "dump.fst", REPO / "dump.vcd") if p.exists()]
    dumps = sorted(cands, key=lambda p: p.stat().st_mtime)
    assert dumps, f"no waveform found under {build} or repo root"
    dest = REPO / "results" / f"week4b_{TOP}_{sim}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


def test_record_via_verilator_and_icarus():
    for sim in ("verilator", "icarus"):
        try:
            _run(sim, waves=bool(int(os.environ.get("WAVES", 0))))
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
            try:
                _run(sim, waves=True)
            except BaseException:
                pass  # rerun outcome only feeds the VCD attempt below
            try:
                vcd = _save_vcd(sim)
            except BaseException:
                vcd = "no-waveform"
            raise AssertionError(f"{TOP} [{sim}] FAILED; VCD {vcd}")
