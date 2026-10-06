"""Cocotb coroutine driving rtl/hello.v (8-bit counter)."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


@cocotb.test()
async def hello_counts(dut):
    """Reset, enable, and check the counter increments by 1 per cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    dut.en.value = 1
    for expected in range(1, 6):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        got = int(dut.count.value)
        assert got == expected, f"count={got}, expected={expected}"
