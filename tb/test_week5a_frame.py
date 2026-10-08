"""Week 5a/5a2 frame_rx cocotb under Verilator + Icarus (via cocotb-test).

One pytest node per (simulator x cocotb case), each with its OWN sim_build
directory (flake isolation: no shared build state between nodes).
Select simulator with WEEK5A_SIM (verilator|icarus|both, default both);
select a single case for debug with WEEK4B_CASE (cocotb testcase name).

Every Verilator build is trace-capable; on failure the root error is
printed and the waveform is copied to results/.
"""

import os
from pathlib import Path

import pytest
from cocotb_test.simulator import run

from tb.sim_retry import flaky_banner, is_infra_crash

REPO = Path(__file__).resolve().parents[1]
RTL = REPO / "rtl"
TB = REPO / "tb"

# case -> (toplevel, verilog sources, cocotb module). The uart_hole case
# runs the test-only integration top (uart_rx -> frame_rx); everything
# else targets frame_rx directly.
CASES = {
    "test_frame_good_and_back_to_back": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_garbage_prefix": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_bad_crc": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_cut_mid_frame": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_counters_saturate": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_sof_in_payload": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_e2e_300_frames": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_resync_two_a5": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_fuzz_corrupt": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_flips88": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_a5_in_crc": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_clear_same_cycle": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_frame_reset_midframe": ("frame_rx", [RTL / "frame_rx.v"], "tb.w5_frame_cocotb"),
    "test_hole_framing_error_midframe": (
        "uart_frame_int",
        [RTL / "uart_rx.v", RTL / "frame_rx.v", TB / "uart_frame_int.v"],
        "tb.w5_hole_cocotb",
    ),
}


def _sims() -> tuple[str, ...]:
    sel = os.environ.get("WEEK5A_SIM", "both").strip().lower()
    if sel == "both":
        return ("verilator", "icarus")
    if sel in ("verilator", "icarus"):
        return (sel,)
    raise ValueError(f"WEEK5A_SIM must be verilator|icarus|both, got {sel!r}")


def _short(case: str) -> str:
    return case.removeprefix("test_frame_").removeprefix("test_hole_")


def _run(sim: str, case: str, waves: bool) -> None:
    top, sources, mod = CASES[case]
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
        waves=True if vtrace else waves,
        sim_build=str(REPO / "sim_build" / f"week5a_{top}_{sim}_{_short(case)}"),
    )


def _save_vcd(sim: str, case: str) -> str:
    top = CASES[case][0]
    build = REPO / "sim_build" / f"week5a_{top}_{sim}_{_short(case)}"
    cands = [p for p in build.rglob("*") if p.suffix in (".vcd", ".fst")]
    cands += [p for p in (REPO / "dump.fst", REPO / "dump.vcd") if p.exists()]
    dumps = sorted(cands, key=lambda p: p.stat().st_mtime)
    assert dumps, f"no waveform found under {build} or repo root"
    dest = REPO / "results" / f"week5a_{top}_{sim}_{_short(case)}_fail{dumps[-1].suffix}"
    import shutil

    shutil.copy(dumps[-1], dest)
    return str(dest)


@pytest.mark.parametrize("sim", _sims())
@pytest.mark.parametrize("case", sorted(CASES))
def test_frame_case(sim: str, case: str):
    override = os.environ.get("WEEK4B_CASE")
    if override and override != case:
        pytest.skip(f"WEEK4B_CASE={override} selects a different case")
        return
    try:
        _run(sim, override or case, waves=bool(int(os.environ.get("WAVES", 0))))
    except BaseException as e:
        if is_infra_crash(e):
            # Dead simulator renders no verdict: one retry, loudly
            # bannered (policy in tb/sim_retry.py).
            print(f"FLAKY-INFRA {case} [{sim}]: {e!r}; retrying once")
            try:
                _run(sim, override or case, waves=True)
            except BaseException as e2:
                print(f"FLAKY-INFRA {case} [{sim}] retry failed: {e2!r}")
            else:
                print(flaky_banner(case, sim, e))
                return
        # Print the root error before the waves rerun swallows it.
        print(f"week5a {case} [{sim}] first attempt failed: {e!r}")
        try:
            _run(sim, override or case, waves=True)
        except BaseException as e2:
            print(f"week5a {case} [{sim}] waves rerun failed: {e2!r}")
        vcd = _save_vcd(sim, case)
        raise AssertionError(f"{case} [{sim}] FAILED; VCD saved to {vcd}")
