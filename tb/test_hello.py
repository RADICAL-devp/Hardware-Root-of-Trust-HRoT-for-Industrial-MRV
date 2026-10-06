"""Hello-world cocotb test run under pytest via Verilator.

Week 1 done-when criterion: this test builds rtl/hello.v with Verilator
and checks the counter through cocotb.
"""

from pathlib import Path

from cocotb_test.simulator import run


def test_hello_via_verilator():
    repo = Path(__file__).resolve().parents[1]
    run(
        verilog_sources=[str(repo / "rtl" / "hello.v")],
        toplevel="hello",
        module="tb.hello_cocotb",
        simulator="verilator",
        toplevel_lang="verilog",
        python_search=[str(repo)],
    )
