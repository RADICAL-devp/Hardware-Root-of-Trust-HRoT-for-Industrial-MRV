"""Week 5b2: the crash policy classifies ONLY process deaths, never verdicts."""

import pytest

from tb.sim_retry import flaky_banner, is_infra_crash


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Process sim_build/x/y terminated with error -11", True),  # SIGSEGV
        ("Process sim_build/x/y terminated with error -6", True),  # SIGABRT
        ("SystemExit('FAILED 1 tests.')", False),
        ("AssertionError: assert 401 == 3", False),
        ("AssertionError: bit 0: crc_err count 0", False),
        ("ValueError: signal dut.foo is X", False),
        ("FAILED tb/test_x.py::test_y - SystemExit", False),
    ],
)
def test_is_infra_crash_precision(text, expected):
    assert is_infra_crash(RuntimeError(text)) is expected


def test_flaky_banner_format():
    line = flaky_banner("test_case", "verilator", RuntimeError("boom -11"))
    assert line.startswith("FLAKY-INFRA-PASS test_case [verilator]")
