"""Week 5b DUT sims under Verilator + Icarus (via cocotb-test).

Power/FSM/chain behavior plus the uart framing-error counter. One pytest
node per (simulator x cocotb case), each with its own sim_build
directory. Simulator filter: WEEK5A_SIM (verilator|icarus|both).
CASES values are (toplevel, sources, module[, parameters]): parameters
override Verilog `parameter`s test-only (FERR_CNT_INIT); production
(top.v, Yosys) must use defaults — checked at those steps. Build dirs are
shared per (toplevel, sources, sim) plus a parameters suffix when present
(same-binary sharing as the other wrappers; params change the build).
"""

import os
from pathlib import Path

import pytest
from cocotb_test.simulator import run

from tb.sim_retry import flaky_banner, is_infra_crash

REPO = Path(__file__).resolve().parents[1]
RTL = REPO / "rtl"
SOURCES = [RTL / s for s in ("power_calc.v", "div_fsm.v", "sqrt_fsm.v")]

CASES = {
    "test_fsm_real_rate": ("power_calc", SOURCES, "tb.w5_powerfsm_cocotb"),
    "test_fsm_overrun_recovery": ("power_calc", SOURCES, "tb.w5_powerfsm_cocotb"),
    "test_fsm_shared_sample_E": ("power_calc", SOURCES, "tb.w5_powerfsm_cocotb"),
    "test_fsm_fullscale_E_drop": ("power_calc", SOURCES, "tb.w5_powerfsm_cocotb"),
    "test_fsm_timing_edges": ("power_calc", SOURCES, "tb.w5_powerfsm_cocotb"),
    "test_fsm_min_spacing": ("power_calc", SOURCES, "tb.w5_powerfsm_cocotb"),
    "test_fsm_overrun_saturate": (
        "power_calc",
        SOURCES,
        "tb.w5_powerfsm_cocotb",
        {"OVERRUN_CNT_INIT": 65534},
    ),
    "test_p2r_overrun_record": (
        "power_record_chain",
        [RTL / s for s in ("power_calc.v", "div_fsm.v", "sqrt_fsm.v", "record_agg.v")]
        + [REPO / "tb" / "power_record_chain.v"],
        "tb.w5_p2r_cocotb",
    ),
    "test_p2r_clear_atomic": (
        "power_record_chain",
        [RTL / s for s in ("power_calc.v", "div_fsm.v", "sqrt_fsm.v", "record_agg.v")]
        + [REPO / "tb" / "power_record_chain.v"],
        "tb.w5_p2r_cocotb",
    ),
    "test_chain_spurious_overrun": (
        "zc_power_chain",
        [RTL / s for s in ("zc_detect.v", "power_calc.v", "div_fsm.v", "sqrt_fsm.v")]
        + [REPO / "tb" / "zc_power_chain.v"],
        "tb.w5_chain_cocotb",
    ),
    "test_ferr_count_clear": ("uart_rx", [RTL / "uart_rx.v"], "tb.w5_ferr_cocotb"),
    "test_ferr_init_saturate": (
        "uart_rx",
        [RTL / "uart_rx.v"],
        "tb.w5_ferr_cocotb",
        {"FERR_CNT_INIT": 65534},
    ),
}


def _sims() -> tuple[str, ...]:
    sel = os.environ.get("WEEK5A_SIM", "both").strip().lower()
    if sel == "both":
        return ("verilator", "icarus")
    if sel in ("verilator", "icarus"):
        return (sel,)
    raise ValueError(f"WEEK5A_SIM must be verilator|icarus|both, got {sel!r}")


def _bdir(sim: str, case: str) -> str:
    top = CASES[case][0]
    params = CASES[case][3] if len(CASES[case]) > 3 else {}
    suffix = "" if not params else "_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))
    return str(REPO / "sim_build" / f"week5b_{top}_{sim}{suffix}")


def _run(sim: str, case: str, waves: bool) -> None:
    top, sources, mod = CASES[case][:3]
    params = CASES[case][3] if len(CASES[case]) > 3 else {}
    os.environ["WEEK4B_TAG"] = sim
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

    top = CASES[case][0]
    build = Path(_bdir(sim, case))
    cands = [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")]
    cands += [p for p in (REPO / "dump.fst", REPO / "dump.vcd") if p.exists()]
    dumps = sorted(cands, key=lambda p: p.stat().st_mtime)
    assert dumps, f"no waveform found under {build} or repo root"
    dest = REPO / "results" / f"week5b_{top}_{sim}_{case}_fail{dumps[-1].suffix}"
    shutil.copy(dumps[-1], dest)
    return str(dest)


@pytest.mark.parametrize("sim", _sims())
@pytest.mark.parametrize("case", sorted(CASES))
def test_powerfsm_case(sim: str, case: str):
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
        print(f"week5b {case} [{sim}] first attempt failed: {e!r}")
        try:
            _run(sim, override or case, waves=True)
        except BaseException as e2:
            print(f"week5b {case} [{sim}] waves rerun failed: {e2!r}")
        vcd = _save_vcd(sim, case)
        raise AssertionError(f"{case} [{sim}] FAILED; VCD saved to {vcd}")
