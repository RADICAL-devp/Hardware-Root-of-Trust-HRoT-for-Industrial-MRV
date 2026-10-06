"""Hello-world cocotb test run under pytest via Verilator.

Week 1 done-when criterion: this test builds rtl/hello.v with Verilator
and checks the counter through cocotb.
"""

from pathlib import Path

from cocotb_test.simulator import run


def test_hello_via_verilator():
    # Trace-capable like every Verilator build: all Mdirs share one
    # sim_build/verilator.o, which must link against trace-enabled model
    # objects (Week 4b2 root cause). No --trace: no dump is captured here.
    repo = Path(__file__).resolve().parents[1]
    run(
        verilog_sources=[str(repo / "rtl" / "hello.v")],
        toplevel="hello",
        module="tb.hello_cocotb",
        simulator="verilator",
        toplevel_lang="verilog",
        python_search=[str(repo)],
        verilog_compile_args=["-CFLAGS", "-DVM_TRACE=1", "-CFLAGS", "-DVM_TRACE_FST=1"],
        make_args=["OPT_FAST=-DVM_TRACE=1 -DVM_TRACE_FST=1"],
        waves=True,
    )
