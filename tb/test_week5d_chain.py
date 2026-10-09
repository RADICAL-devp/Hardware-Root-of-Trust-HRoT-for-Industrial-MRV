"""Week 5d1 DUT sims under Verilator + Icarus (via cocotb-test).

chain_ctrl hash-chain sequencer driving one sha256_wrap through
tb/chain_wrap_pair.v (unit harness; top.v owns the mux in 5d2), plus the
standalone win_counter (halt-at-max, test-only CNT_INIT). One pytest
node per (simulator x cocotb case), each with its own sim_build
directory. Simulator filter: WEEK5D_SIM (verilator|icarus|both).
"""

import os
from pathlib import Path

import pytest
from cocotb_test.simulator import run

from tb.sim_retry import flaky_banner, is_infra_crash

REPO = Path(__file__).resolve().parents[1]
RTL = REPO / "rtl"
TP = RTL / "third_party" / "secworks-sha256" / "src" / "rtl"
SECWORKS = [TP / s for s in ("sha256_core.v", "sha256_w_mem.v", "sha256_k_constants.v")]
CHAIN_SOURCES = (
    [RTL / "chain_ctrl.v", RTL / "sha256_wrap.v"] + SECWORKS + [REPO / "tb" / "chain_wrap_pair.v"]
)
COUNTER_SOURCES = [RTL / "win_counter.v"]

CASES = {
    "test_chain_genesis_link": ("chain_wrap_pair", CHAIN_SOURCES, "tb.w5d_chain_cocotb"),
    "test_chain_tomb_link": ("chain_wrap_pair", CHAIN_SOURCES, "tb.w5d_chain_cocotb"),
    "test_chain_rejected_frame": ("chain_wrap_pair", CHAIN_SOURCES, "tb.w5d_chain_cocotb"),
    "test_chain_empty_samples": ("chain_wrap_pair", CHAIN_SOURCES, "tb.w5d_chain_cocotb"),
    "test_chain_overflow_abort": ("chain_wrap_pair", CHAIN_SOURCES, "tb.w5d_chain_cocotb"),
    "test_counter_mono": ("win_counter", COUNTER_SOURCES, "tb.w5d_chain_cocotb"),
    "test_counter_halt": (
        "win_counter",
        COUNTER_SOURCES,
        "tb.w5d_chain_cocotb",
        {"CNT_INIT": 4294967293},
    ),
    "test_counter_reset": ("win_counter", COUNTER_SOURCES, "tb.w5d_chain_cocotb"),
}


def _sims() -> tuple[str, ...]:
    sel = os.environ.get("WEEK5D_SIM", "both").strip().lower()
    if sel == "both":
        return ("verilator", "icarus")
    if sel in ("verilator", "icarus"):
        return (sel,)
    raise ValueError(f"WEEK5D_SIM must be verilator|icarus|both, got {sel!r}")


def _bdir(sim: str, case: str) -> str:
    top = CASES[case][0]
    params = CASES[case][3] if len(CASES[case]) > 3 else {}
    suffix = "" if not params else "_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))
    return str(REPO / "sim_build" / f"week5d_{top}_{sim}{suffix}")


def _run(sim: str, case: str, waves: bool) -> None:
    top, sources, mod = CASES[case][:3]
    params = CASES[case][3] if len(CASES[case]) > 3 else {}
    os.environ["WEEK4B_TAG"] = sim
    # Crash forensics (week5d1b): a segfaulting sim prints a Python
    # traceback to stderr instead of dying silent (no verdict either
    # way — the retry policy is unchanged).
    os.environ["PYTHONFAULTHANDLER"] = "1"
    vtrace = sim == "verilator"
    plus = ["--trace"] if (vtrace and waves) else []
    cflags = ["-CFLAGS", "-DVM_TRACE=1", "-CFLAGS", "-DVM_TRACE_FST=1"] if vtrace else []
    mk = ["OPT_FAST=-DVM_TRACE=1 -DVM_TRACE_FST=1"] if vtrace else []
    run(
        verilog_sources=[str(s) for s in sources],
        toplevel=top,
        module=mod,
        simulator=sim,
        testcase=case,
        plus_args=plus,
        verilog_compile_args=cflags,
        make_args=mk,
        toplevel_lang="verilog",
        python_search=[str(REPO)],
        includes=[str(REPO / "rtl")],
        parameters=params,
        waves=True if vtrace else waves,
        sim_build=_bdir(sim, case),
    )


def _save_vcd(sim: str, case: str) -> str:
    import shutil

    build = Path(_bdir(sim, case))
    cands = [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")]
    cands += [p for p in (REPO / "dump.fst", REPO / "dump.vcd") if p.exists()]
    dumps = sorted(cands, key=lambda p: p.stat().st_mtime)
    assert dumps, f"no waveform found under {build} or repo root"
    dest = REPO / "results" / f"week5d_{CASES[case][0]}_{sim}_{case}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


@pytest.mark.parametrize("sim", _sims())
@pytest.mark.parametrize("case", sorted(CASES))
def test_chain_case(sim: str, case: str):
    override = os.environ.get("WEEK4B_CASE")
    if override and override != case:
        pytest.skip(f"WEEK4B_CASE={override} selects a different case")
        return
    try:
        _run(sim, override or case, waves=bool(int(os.environ.get("WAVES", 0))))
    except BaseException as e:
        if is_infra_crash(e):
            # Dead simulator renders no verdict: one retry, loudly bannered.
            print(f"FLAKY-INFRA {case} [{sim}]: {e!r}; retrying once")
            try:
                _run(sim, override or case, waves=True)
            except BaseException as e2:
                print(f"FLAKY-INFRA {case} [{sim}] retry failed: {e2!r}")
            else:
                print(flaky_banner(case, sim, e))
                return
        print(f"week5d {case} [{sim}] first attempt failed: {e!r}")
        try:
            _run(sim, override or case, waves=True)
        except BaseException as e2:
            print(f"week5d {case} [{sim}] waves rerun failed: {e2!r}")
        vcd = _save_vcd(sim, case)
        raise AssertionError(f"{case} [{sim}] FAILED; VCD saved to {vcd}")
