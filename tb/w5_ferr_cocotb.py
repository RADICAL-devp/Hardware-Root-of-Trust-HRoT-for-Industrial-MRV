"""Week 5b cocotb: uart_rx framing_err_cnt (toplevel=uart_rx).

framing_err_cnt is a saturating 16-bit health counter over framing_error
pulses (bad stop bits): +1 per error, sticky at 0xFFFF, zeroed by the
cnt_clear sync strobe (reset value FERR_CNT_INIT, default 0). Same-cycle
clear+error resolves increment-wins (clear zeroes, the error counts from
zero -> 1). Saturation past FFFF is proven via the test-only FERR_CNT_INIT
override (cocotb-test parameters=), never overridden by top.v or synthesis.
"""

import cocotb
from cocotb.triggers import RisingEdge

from tb.w4b_common import read_sig, reset_dut, settle, start_clock
from tb.w4b_uart_cocotb import send_byte, send_bytes


async def read_ferr(dut) -> int:
    await settle()
    return read_sig(dut.framing_err_cnt, 16)


async def pulse_clear(dut):
    dut.cnt_clear.value = 1
    await RisingEdge(dut.clk)
    await settle()
    dut.cnt_clear.value = 0
    await RisingEdge(dut.clk)
    await settle()


@cocotb.test()
async def test_ferr_count_clear(dut):
    """Errors count; clear zeroes; resume counts; same-cycle error wins."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    assert await read_ferr(dut) == 0
    await send_bytes(dut, bytes([0x11, 0x22, 0x33]))
    assert await read_ferr(dut) == 0, "good bytes must not count"
    await send_byte(dut, 0x44, stop=0)
    assert await read_ferr(dut) == 1
    await send_byte(dut, 0x55, stop=0)
    await send_byte(dut, 0x66, stop=0)
    assert await read_ferr(dut) == 3
    await pulse_clear(dut)
    assert await read_ferr(dut) == 0, "clear must zero the counter"
    await send_byte(dut, 0x77, stop=0)
    assert await read_ferr(dut) == 1, "counting must resume after clear"
    # NOTE: no same-cycle error+clear case here: at line-timing granularity
    # the clear window cannot end exactly on the completion edge, so a wide
    # window would erase the just-counted error after the fact. The
    # increment-wins rule (shared code pattern) is proven with exact-cycle
    # control in test_frame_clear_same_cycle instead.
    cocotb.log.info("ferr: count/clear/resume exact")


@cocotb.test()
async def test_ferr_init_saturate(dut):
    """FERR_CNT_INIT override + saturation stickiness (test-only path).

    Runs with parameters={"FERR_CNT_INIT": 65534}: reset lands at FFFE,
    two errors reach FFFF, the third sticks (a wrap would read 0x0000).
    """
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    assert await read_ferr(dut) == 0xFFFE, "INIT override did not take"
    await send_byte(dut, 0x01, stop=0)
    assert await read_ferr(dut) == 0xFFFF
    await send_byte(dut, 0x02, stop=0)
    await send_byte(dut, 0x03, stop=0)
    assert await read_ferr(dut) == 0xFFFF, "saturation must stick, not wrap"
    await pulse_clear(dut)
    assert await read_ferr(dut) == 0, "clear zeroes even from saturated"
    cocotb.log.info("ferr: INIT override + saturation sticky")
