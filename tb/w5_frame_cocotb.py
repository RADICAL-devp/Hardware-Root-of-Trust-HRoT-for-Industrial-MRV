"""Week 5a cocotb: frame_rx byte-stream L0 framer (toplevel=frame_rx).

The golden (tb/golden.py parse_l0_stream) is the byte-model contract: this
test drives BYTES into frame_rx (one byte per data_valid pulse, as uart_rx
would emit) and compares emitted (counter, V, I) against the golden parse.
Covers: good/back-to-back frames, garbage prefix, truncated-tail buffering,
bad CRC (crc_err, frame dropped), SOF byte inside payload, cut stream
mid-frame resync, and a 300-frame seeded end-to-end run.

Driving convention (matches RTL contract): data_in/data_valid are
REGISTERED — set before a rising edge, read after (+1 ns settle). Outputs
frame_valid/crc_err are 1-clk pulses with latched counter/v_q15/i_q15.
"""

import cocotb
from cocotb.triggers import Event, RisingEdge

from edge.framing import encode_frame
from tb.golden import parse_l0_stream
from tb.w4b_common import read_sig, reset_dut, settle, sine_codes, start_clock

CLK_NS = 10


async def feed_bytes(dut, data: bytes):
    """Drive one byte per clock: data_valid high for exactly 1 cycle each."""
    for byte in data:
        dut.data_in.value = byte
        dut.data_valid.value = 1
        await RisingEdge(dut.clk)
        await settle()
        dut.data_valid.value = 0
        await RisingEdge(dut.clk)
        await settle()
    dut.data_in.value = 0


async def monitor(dut, captured, errors, stop_ev):
    """Capture every frame_valid frame and crc_err pulse (edge-polled)."""
    while not stop_ev.is_set():
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.frame_valid, 1):
            c = read_sig(dut.frame_counter, 32)
            v = read_sig(dut.frame_v, 16, signed=True)
            i = read_sig(dut.frame_i, 16, signed=True)
            captured.append((c, v, i))
        if read_sig(dut.crc_err, 1):
            errors.append(1)


async def run_stream(dut, stream: bytes):
    """Feed a stream under monitor; return (captured frames, crc_err count)."""
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    await feed_bytes(dut, stream)
    for _ in range(5):
        await RisingEdge(dut.clk)
    await settle()
    stop_ev.set()
    await mon
    return captured, errors


async def reset_frame_rx(dut):
    dut.data_in.value = 0
    dut.data_valid.value = 0
    await reset_dut(dut)
    await settle()


@cocotb.test()
async def test_frame_good_and_back_to_back(dut):
    """Good frames byte-exact; back-to-back needs no idle gap."""
    start_clock(dut)
    await reset_frame_rx(dut)
    frames = [encode_frame(k, 1000 + k, -500 - k) for k in range(8)]
    captured, errors = await run_stream(dut, b"".join(frames))
    assert errors == [], f"unexpected crc_err on good frames: {errors}"
    expect = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(b"".join(frames))]
    assert captured == expect == [(k, 1000 + k, -500 - k) for k in range(8)]


@cocotb.test()
async def test_frame_garbage_prefix(dut):
    """Garbage bytes before SOF are ignored; trailing partial is buffered."""
    start_clock(dut)
    await reset_frame_rx(dut)
    frames = [encode_frame(k, k, -k) for k in range(3)]
    tail = encode_frame(99, 7, -7)
    stream = b"\x00\xff\xa5\xa5\x00\xffgarbage!" + b"".join(frames) + tail[:5]
    captured, errors = await run_stream(dut, stream)
    expect = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(stream)]
    assert captured == expect == [(0, 0, 0), (1, 1, -1), (2, 2, -2)]
    # Complete the buffered tail: frame 99 must now emit.
    captured2, _ = await run_stream(dut, tail[5:])
    assert captured2 == [(99, 7, -7)]
    # Golden on the concatenated stream agrees.
    full = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(stream + tail[5:])]
    assert full == [(0, 0, 0), (1, 1, -1), (2, 2, -2), (99, 7, -7)]


@cocotb.test()
async def test_frame_bad_crc(dut):
    """Bad-CRC frame dropped with crc_err; neighbours survive byte-exact."""
    start_clock(dut)
    await reset_frame_rx(dut)
    good0 = encode_frame(10, 111, 222)
    bad = bytearray(encode_frame(11, 333, -444))
    bad[9] ^= 0xFF  # corrupt CRC low byte
    good2 = encode_frame(12, 555, 666)
    stream = bytes(good0) + bytes(bad) + bytes(good2)
    captured, errors = await run_stream(dut, stream)
    assert len(errors) == 1, f"expected exactly one crc_err, got {len(errors)}"
    expect = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(stream)]
    assert captured == expect == [(10, 111, 222), (12, 555, 666)]


@cocotb.test()
async def test_frame_cut_mid_frame(dut):
    """Stream cut mid-frame: partial held, next SOF resyncs, CRC rejects junk."""
    start_clock(dut)
    await reset_frame_rx(dut)
    partial = encode_frame(7, 111, 222)[:4]
    good = encode_frame(8, 333, -444)
    captured, errors = await run_stream(dut, partial + good)
    # Golden: partial prefix bytes cannot form a frame; only frame 8 parses
    # unless the 4 partial bytes + first bytes of `good` accidentally CRC-pass
    # (they do not for these vectors — pinned by the equality below).
    expect = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(partial + good)]
    assert captured == expect
    assert (8, 333, -444) in captured


@cocotb.test()
async def test_frame_sof_in_payload(dut):
    """0xA5 bytes inside V/I payload must not confuse the framer."""
    start_clock(dut)
    await reset_frame_rx(dut)
    frames = [encode_frame(k, 0x25A5 - 2 * k, -0x5B60 + k) for k in range(5)]
    assert any(b"\xa5" in f[1:9] for f in frames)  # precondition: SOF in payload
    stream = b"".join(frames)
    captured, errors = await run_stream(dut, stream)
    assert errors == []
    expect = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(stream)]
    assert captured == expect == [(k, 0x25A5 - 2 * k, -0x5B60 + k) for k in range(5)]


@cocotb.test()
async def test_frame_e2e_300_frames(dut):
    """300 seeded L0 frames through the framer: codes byte-exact end to end."""
    start_clock(dut)
    await reset_frame_rx(dut)
    v, i = sine_codes(300, seed=99)
    stream = b"".join(encode_frame(k, v[k], i[k]) for k in range(300))
    captured, errors = await run_stream(dut, stream)
    assert errors == []
    expect = [(k, v[k], i[k]) for k in range(300)]
    assert captured == expect
    cocotb.log.info("e2e: 300 frames exact through frame_rx")
