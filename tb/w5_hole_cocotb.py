"""Week 5a2 cocotb: uart_rx -> frame_rx hole behavior (toplevel=uart_frame_int).

Defines the framing_error contract: a bad stop bit deletes exactly one
byte from the stream. frame_rx sees the holed stream, rejects the
straddling candidate via CRC (crc_err), and resyncs on the next SOF.
Straddling frames are lost; neighbors survive; no latch-up, no spurious
frame. Expected frames come from parse_l0_stream over the bytes uart_rx
actually emitted (the holed stream), so the whole byte path is checked.
"""

import cocotb
from cocotb.triggers import Event, RisingEdge

from edge.framing import encode_frame
from tb.golden import parse_l0_stream
from tb.w4b_common import read_sig, reset_dut, settle, start_clock
from tb.w4b_uart_cocotb import send_byte, send_bytes
from tb.w5_frame_cocotb import count_stream


async def monitor(dut, captured, crcs, ferrs, stop_ev):
    """Capture frame_rx frames, crc_err and uart framing_error pulses."""
    while not stop_ev.is_set():
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.frame_valid, 1):
            captured.append(
                (
                    read_sig(dut.frame_counter, 32),
                    read_sig(dut.frame_v, 16, signed=True),
                    read_sig(dut.frame_i, 16, signed=True),
                )
            )
        if read_sig(dut.crc_err, 1):
            crcs.append(1)
        if read_sig(dut.framing_error, 1):
            ferrs.append(1)


@cocotb.test()
async def test_hole_framing_error_midframe(dut):
    """Bad stop bit on one mid-frame payload byte: 1-byte hole, then resync."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    f0 = encode_frame(60, 1000, -1000)
    f1 = encode_frame(61, 2000, -2000)
    f2 = encode_frame(62, 3000, -3000)
    stream = bytes(f0) + bytes(f1) + bytes(f2)
    hole = len(f0) + 4  # drop f1's 5th byte (an I-payload byte)
    captured, crcs, ferrs, stop_ev = [], [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, crcs, ferrs, stop_ev))
    await send_bytes(dut, stream[:hole])
    await send_byte(dut, stream[hole], stop=0)  # bad stop: byte deleted
    await send_bytes(dut, stream[hole + 1 :])
    for _ in range(30):  # drain: one UART byte = 160 clocks here
        await RisingEdge(dut.clk)
    await settle()
    stop_ev.set()
    await mon
    assert len(ferrs) == 1, f"expected one framing_error, got {len(ferrs)}"
    holed = stream[:hole] + stream[hole + 1 :]
    expect = [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(holed)]
    assert expect == [(60, 1000, -1000), (62, 3000, -3000)], f"golden: {expect}"
    assert captured == expect, f"RTL: {captured}"
    assert len(crcs) == count_stream(holed)[0]
    assert (read_sig(dut.crc_err_cnt, 16), read_sig(dut.resync_cnt, 16)) == count_stream(holed)
    assert read_sig(dut.cnt_saturated, 1) == 0  # small counts: passthrough wired, clear
    cocotb.log.info("hole: 1 byte deleted, straddling frame lost, stream resynced")
