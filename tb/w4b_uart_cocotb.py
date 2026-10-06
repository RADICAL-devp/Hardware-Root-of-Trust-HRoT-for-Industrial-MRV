"""Week 4b cocotb: uart_rx bit-level signaling (toplevel=uart_rx).

The golden (tb/golden.py parse_l0_stream) models bytes, not bits — so these
tests drive the LINE (start/stop sampling, baud error) and compare the
emitted BYTES against the golden parse. Covers: good frames, back-to-back
frames, bad stop bit (dropped + framing_error), frame cut mid-byte (garbage
rejected by CRC, resync on next SOF), baud error within ±2% tolerance,
reset mid-frame, and a 300-frame L0 end-to-end byte-exact run.

Phase safety: rx transitions happen 1 ns after clock edges (9 ns setup),
so sampling is race-free in both simulators; baud-error drift keeps ≥7 ns
setup (asserted by the passing bytes, not assumed).
"""

import cocotb
from cocotb.triggers import Event, RisingEdge, Timer

from edge.framing import encode_frame
from tb.golden import parse_l0_stream
from tb.w4b_common import reset_dut, settle, sine_codes, start_clock

CLK_PER_BIT = 16
BIT_NS = 16 * 10.0  # [ns] nominal bit time at the 10 ns TB clock


async def send_byte(dut, byte, bit_ns=BIT_NS, stop=1, idle_after_ns=0.0):
    """Transmit one 8N1 byte LSB-first; stop=0 injects a bad stop bit."""
    dut.rx.value = 0
    await Timer(bit_ns, unit="ns")
    for b in range(8):
        dut.rx.value = (byte >> b) & 1
        await Timer(bit_ns, unit="ns")
    dut.rx.value = stop
    await Timer(bit_ns, unit="ns")
    if not stop:
        dut.rx.value = 1
    if idle_after_ns:
        await Timer(idle_after_ns, unit="ns")


async def send_bytes(dut, data, **kw):
    for byte in data:
        await send_byte(dut, byte, **kw)


async def monitor(dut, captured, errors, stop_ev):
    """Capture every data_valid byte and framing_error pulse (edge-polled)."""
    while not stop_ev.is_set():
        await RisingEdge(dut.clk)
        await settle()
        if int(dut.data_valid.value):
            captured.append(int(dut.data.value))
        if int(dut.framing_error.value):
            errors.append(1)


@cocotb.test()
async def test_uart_good_and_back_to_back(dut):
    """Good frames byte-exact; back-to-back needs no idle gap."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    frames = [encode_frame(k, 1000 + k, -500 - k) for k in range(8)]
    await send_bytes(dut, b"".join(frames))  # zero gap between frames
    await Timer(3 * BIT_NS, unit="ns")
    stop_ev.set()
    await mon
    assert errors == []
    got = parse_l0_stream(bytes(captured))
    assert [(s.counter, s.v_q15, s.i_q15) for s in got] == [
        (k, 1000 + k, -500 - k) for k in range(8)
    ]


@cocotb.test()
async def test_uart_bad_stop_bit(dut):
    """Bad stop bit: byte dropped + framing_error, stream resyncs after."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    good = encode_frame(41, 1234, -321)
    # Corrupt the stop bit of byte 4 (an I payload byte).
    for k, byte in enumerate(good):
        await send_byte(dut, byte, stop=0 if k == 4 else 1)
    await send_bytes(dut, encode_frame(42, 2000, 1000))
    await Timer(3 * BIT_NS, unit="ns")
    stop_ev.set()
    await mon
    assert len(errors) == 1, f"expected exactly one framing error, got {len(errors)}"
    got = parse_l0_stream(bytes(captured))
    # Frame 41 lost its byte (CRC gate); frame 42 arrives intact.
    assert [(s.counter, s.v_q15, s.i_q15) for s in got] == [(42, 2000, 1000)]


@cocotb.test()
async def test_uart_cut_mid_byte(dut):
    """Line cut mid-byte (idles high): resyncs on next SOF, CRC rejects junk."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    partial = encode_frame(7, 111, 222)[:4]
    await send_bytes(dut, partial)
    dut.rx.value = 0  # start bit of a byte that never finishes...
    await Timer(BIT_NS, unit="ns")
    for b in (1, 0, 1):
        dut.rx.value = b
        await Timer(BIT_NS, unit="ns")
    dut.rx.value = 1  # ...cut: line idles high mid-byte
    # The receiver is still clocking out the cut byte (~9.5 bit-times from
    # its start bit); only after it returns to IDLE can the next frame sync.
    # This is real UART behavior (a frame arriving mid-byte overruns), not a
    # resync failure — so wait out the full byte before the good frame.
    await Timer(12 * BIT_NS, unit="ns")
    await send_bytes(dut, encode_frame(8, 333, -444))
    await Timer(3 * BIT_NS, unit="ns")
    stop_ev.set()
    await mon
    got = parse_l0_stream(bytes(captured))
    assert [(s.counter, s.v_q15, s.i_q15) for s in got] == [(8, 333, -444)]


@cocotb.test()
async def test_uart_baud_tolerance(dut):
    """Baud error within tolerance: bytes still exact (samples in-cell).

    Error list from BIT_ERRS env (default ±2 % and ±3 %, all pinned passing).
    Measured pass/fail edge (Week 4b2, both sims agree): -3.5 % passes, -4 %
    fails (every byte framing-errors); +5 % passes, +5.5 % fails. The pinned
    ±2 % tolerance keeps >= 1.5 pp margin on the tight (fast) side.
    """
    import os

    start_clock(dut)
    await reset_dut(dut)
    await settle()
    errs = [float(x) for x in os.environ.get("BIT_ERRS", "-0.03,-0.02,0.02,0.03").split(",")]
    for err in errs:
        captured, errors, stop_ev = [], [], Event()
        mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
        frames = [encode_frame(100 + k, -7000 + k, 8000 - k) for k in range(4)]
        await send_bytes(dut, b"".join(frames), bit_ns=BIT_NS * (1 + err))
        await Timer(3 * BIT_NS, unit="ns")
        stop_ev.set()
        await mon
        assert errors == [], f"baud {err:+}: framing errors {errors}"
        got = parse_l0_stream(bytes(captured))
        assert [(s.counter, s.v_q15, s.i_q15) for s in got] == [
            (100 + k, -7000 + k, 8000 - k) for k in range(4)
        ], f"baud {err:+}: byte mismatch"


@cocotb.test()
async def test_uart_reset_mid_frame(dut):
    """Reset mid-frame: silent under reset, exact bytes after."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    frame = encode_frame(9, 555, -666)
    await send_bytes(dut, frame[:5])
    await Timer(2 * BIT_NS, unit="ns")  # let the 5 pre-reset bytes land
    n_pre, e_pre = len(captured), len(errors)
    assert n_pre == 5, f"pre-reset bytes missing: {captured}"
    dut.rst_n.value = 0
    dut.rx.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    # Nothing new may escape while reset is held (partial state is dropped).
    assert len(captured) == n_pre, f"spurious bytes under reset: {captured[n_pre:]}"
    assert len(errors) == e_pre
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await settle()
    await send_bytes(dut, frame)
    await Timer(3 * BIT_NS, unit="ns")
    stop_ev.set()
    await mon
    got = parse_l0_stream(bytes(captured))
    assert [(s.counter, s.v_q15, s.i_q15) for s in got] == [(9, 555, -666)]


@cocotb.test()
async def test_uart_e2e_300_frames(dut):
    """300 L0 sine-sample frames over the line: codes byte-exact end to end."""
    start_clock(dut)
    await reset_dut(dut)
    await settle()
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    v, i = sine_codes(300, seed=99)
    expect = [(k, v[k], i[k]) for k in range(300)]
    stream = b"".join(encode_frame(k, v[k], i[k]) for k in range(300))
    await send_bytes(dut, stream)
    await Timer(3 * BIT_NS, unit="ns")
    stop_ev.set()
    await mon
    assert errors == []
    got = parse_l0_stream(bytes(captured))
    assert [(s.counter, s.v_q15, s.i_q15) for s in got] == expect
    cocotb.log.info("e2e: 300 frames / 3300 bytes exact through bit-level RX")
