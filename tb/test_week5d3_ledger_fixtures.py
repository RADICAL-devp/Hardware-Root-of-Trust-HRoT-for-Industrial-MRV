"""Week 5d3 RTL ledger fixture generation (Verilator + Icarus, via cocotb-test).

One pytest node per (simulator x case); SLOW_CASES run in test-full/repro
only. Each case drives top.v through the w5d_ledger_cocotb generator,
which signs the DUT output through ledger.writer and byte-compares the
result to the committed fixture in tests/fixtures/ (oracle; bootstrap on
first clone). Simulator filter: WEEK5D_SIM.
"""

import os
from pathlib import Path

import pytest
from cocotb_test.simulator import run

from tb.sim_retry import flaky_banner, is_infra_crash

REPO = Path(__file__).resolve().parents[1]
RTL = REPO / "rtl"
TOP_SOURCES = [
    RTL / s
    for s in (
        "top.v",
        "uart_rx.v",
        "frame_rx.v",
        "zc_detect.v",
        "power_calc.v",
        "div_fsm.v",
        "sqrt_fsm.v",
        "record_agg.v",
        "chain_ctrl.v",
        "win_counter.v",
        "hmac_core.v",
        "sha256_wrap.v",
        "skid_buf.v",
    )
] + [
    RTL / "third_party" / "secworks-sha256" / "src" / "rtl" / s
    for s in ("sha256_core.v", "sha256_w_mem.v", "sha256_k_constants.v")
]

MOD = "tb.w5d_ledger_cocotb"

# case -> (toplevel, sources, module, testcase[, parameters[, env]])
CASES = {
    "ledger_fixture_clean": ("top", TOP_SOURCES, MOD, "test_top_ledger_clean_fixture"),
    "ledger_fixture_overflow": (
        "top",
        TOP_SOURCES,
        MOD,
        "test_top_ledger_overflow_fixture",
        {"SKID_DEPTH": 4},
    ),
}

SLOW_CASES = {"ledger_fixture_clean", "ledger_fixture_overflow"}


def _sims() -> tuple[str, ...]:
    sel = os.environ.get("WEEK5D_SIM", "both").strip().lower()
    if sel == "both":
        return ("verilator", "icarus")
    if sel in ("verilator", "icarus"):
        return (sel,)
    raise ValueError(f"WEEK5D_SIM must be verilator|icarus|both, got {sel!r}")


def _params() -> list:
    out = []
    for sim in _sims():
        for case in sorted(CASES):
            marks = [pytest.mark.slow] if case in SLOW_CASES else []
            out.append(pytest.param(sim, case, marks=marks))
    return out


def _bdir(sim: str, case: str) -> str:
    top = CASES[case][0]
    params = CASES[case][4] if len(CASES[case]) > 4 else {}
    suffix = "" if not params else "_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))
    return str(REPO / "sim_build" / f"week5d3_{top}_{sim}{suffix}")


def _run(sim: str, case: str, waves: bool) -> None:
    top, sources, module, testcase = CASES[case][:4]
    params = CASES[case][4] if len(CASES[case]) > 4 else {}
    env = CASES[case][5] if len(CASES[case]) > 5 else {}
    os.environ["WEEK4B_TAG"] = sim
    os.environ["PYTHONFAULTHANDLER"] = "1"
    for k, v in env.items():
        os.environ[k] = v
    vtrace = sim == "verilator"
    plus = ["--trace"] if (vtrace and waves) else []
    cflags = ["-CFLAGS", "-DVM_TRACE=1", "-CFLAGS", "-DVM_TRACE_FST=1"] if vtrace else []
    mk = ["OPT_FAST=-DVM_TRACE=1 -DVM_TRACE_FST=1"] if vtrace else []
    run(
        verilog_sources=[str(s) for s in sources],
        toplevel=top,
        module=module,
        simulator=sim,
        testcase=testcase,
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
    dest = REPO / "results" / f"week5d3_{CASES[case][0]}_{sim}_{case}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


@pytest.mark.parametrize("sim,case", _params())
def test_fixture_case(sim: str, case: str):
    override = os.environ.get("WEEK4B_CASE")
    if override and override != case:
        pytest.skip(f"WEEK4B_CASE={override} selects a different case")
        return
    try:
        _run(sim, override or case, waves=bool(int(os.environ.get("WAVES", 0))))
    except BaseException as e:
        if is_infra_crash(e):
            print(f"FLAKY-INFRA {case} [{sim}]: {e!r}; retrying once")
            try:
                _run(sim, override or case, waves=True)
            except BaseException as e2:
                print(f"FLAKY-INFRA {case} [{sim}] retry failed: {e2!r}")
            else:
                print(flaky_banner(case, sim, e))
                return
        print(f"week5d3 {case} [{sim}] first attempt failed: {e!r}")
        try:
            _run(sim, override or case, waves=True)
        except BaseException as e2:
            print(f"week5d3 {case} [{sim}] waves rerun failed: {e2!r}")
        vcd = _save_vcd(sim, case)
        raise AssertionError(f"{case} [{sim}] FAILED; VCD saved to {vcd}")
