"""Week 5a/5a2 cocotb: frame_rx byte-stream L0 framer (toplevel=frame_rx).

The golden (tb/golden.py parse_l0_stream) is the byte-model contract: this
test drives BYTES into frame_rx (one byte per data_valid pulse, as uart_rx
would emit) and compares emitted (counter, V, I) against the golden parse.
Covers: good/back-to-back frames, garbage prefix, truncated-tail buffering,
bad CRC (crc_err, frame dropped), SOF byte inside payload, cut stream
mid-frame resync, 300-frame seeded end-to-end, double-A5 resync (valid
frame at the second inner SOF), seeded corruption fuzz, all 88 single-bit
flips, 0xA5 inside CRC bytes, and reset mid-frame.

Driving convention (matches RTL contract): data_in/data_valid are
REGISTERED — set before a rising edge, read after (+1 ns settle). Outputs
frame_valid/crc_err are 1-clk pulses with latched counter/v_q15/i_q15.
crc_err_cnt/resync_cnt are 16-bit saturating health counters (telemetry,
not policy: monotonicity lives in the Python receiver).
"""

import random

import cocotb
from cocotb.triggers import Event, RisingEdge

from edge.framing import decode_frame, encode_frame
from tb.golden import parse_l0_stream
from tb.w4b_common import read_sig, reset_dut, settle, sine_codes, start_clock

CLK_NS = 10
FUZZ_STREAMS = 64  # [count] fuzz streams per sim run (seed 42 below)
FUZZ_MIN_BYTES = 320  # [bytes] minimum stream length before cut-off


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


async def feed_bytes_fast(dut, data: bytes):
    """Back-to-back bytes at 1 byte/cycle (data_valid held high)."""
    for byte in data:
        dut.data_in.value = byte
        dut.data_valid.value = 1
        await RisingEdge(dut.clk)
        await settle()
    dut.data_valid.value = 0
    dut.data_in.value = 0
    await RisingEdge(dut.clk)
    await settle()


async def run_stream_nomonitor(dut, stream: bytes):
    """Feed a stream with no monitor (the counters are the observation)."""
    await feed_bytes_fast(dut, stream)
    for _ in range(5):
        await RisingEdge(dut.clk)
    await settle()


async def reset_frame_rx(dut):
    dut.data_in.value = 0
    dut.data_valid.value = 0
    await reset_dut(dut)
    await settle()


async def read_counters(dut) -> tuple[int, int]:
    """Read (crc_err_cnt, resync_cnt) [counts] after settling."""
    await settle()
    return (read_sig(dut.crc_err_cnt, 16), read_sig(dut.resync_cnt, 16))


def count_stream(data: bytes) -> tuple[int, int]:
    """Rejection/resync counts derived from parse_l0_stream (single golden).

    Walks SOF positions with the D-01 advance rule (+1 past a bad window,
    +11 past a good one); each window's CRC verdict comes from
    parse_l0_stream itself on the 11-byte slice, so frames AND counts
    share the ONE golden CRC implementation (no second opinion that could
    drift). A rejected window resyncs iff its bytes 1..10 hold an inner
    SOF (prefix reuse in RTL terms).
    """
    from tb.golden import FRAME_LEN, SOF

    rejects = resyncs = 0
    pos, n = 0, len(data)
    while True:
        try:
            p = data.index(SOF, pos)
        except ValueError:
            return (rejects, resyncs)
        if p + FRAME_LEN > n:
            return (rejects, resyncs)
        if parse_l0_stream(data[p : p + FRAME_LEN]):
            pos = p + FRAME_LEN
        else:
            rejects += 1
            if SOF in data[p + 1 : p + FRAME_LEN]:
                resyncs += 1
            pos = p + 1


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
    assert await read_counters(dut) == (0, 0)
    cocotb.log.info("e2e: 300 frames exact through frame_rx")


@cocotb.test()
async def test_frame_resync_two_a5(dut):
    """Bad-CRC candidate holding TWO 0xA5; valid frame at the second.

    Exercises first-SOF-wins plus iterative rescan across two completions:
    C1 (bogus SOF) fails -> prefix from the first inner A5 -> that
    candidate fails too -> prefix from the second A5 == real frame start.
    Two geometries: (p1, p2) = (4, 8) and (1, 5), covering rescan shifts
    of different lengths (the p1 = 1 geometry pins the longest shift).
    """
    start_clock(dut)
    for p1, p2 in ((4, 8), (1, 5)):
        await reset_frame_rx(dut)
        stream, expect = build_two_a5(p1, p2)
        captured, errors = await run_stream(dut, stream)
        assert captured == expect, f"p1={p1}: RTL mismatch {captured}"
        assert len(errors) == 2, f"p1={p1}: expected two crc_err, got {len(errors)}"
        assert await read_counters(dut) == (2, 2), f"p1={p1}"
    cocotb.log.info("two_a5: both geometries resync exactly like the golden")


def build_two_a5(p1: int, p2: int) -> tuple[bytes, list[tuple[int, int, int]]]:
    """Build a double-A5 resync vector: (stream, expected frames).

    Bogus SOF + filler (one A5 at p1, F2's SOF at p2) + real frames F2, F3;
    both intermediate 11-byte windows are CRC-bad (seeded search, asserts).
    Shared by the resync and saturation tests (single construction).
    """
    f2 = encode_frame(200, 1234, -4321)
    f3 = encode_frame(201, 111, 222)
    for attempt in range(1000):
        rng = random.Random(10000 + attempt)
        head = bytearray(rng.randbytes(p2 - 1))
        if any(b == 0xA5 for b in head):
            continue  # exactly one A5 at p1, none before p2 otherwise
        head[p1 - 1] = 0xA5
        cand = bytes([0xA5]) + bytes(head) + f2
        if decode_frame(cand[:11], 0) is not None:
            continue  # need C1 CRC-bad
        if decode_frame(cand[p1 : p1 + 11], 0) is not None:
            continue  # need the p1 candidate CRC-bad too
        stream = cand + f3
        # Golden must see exactly [F2, F3]: proves both intermediates
        # CRC-fail (and guards against A5 bytes inside F2's head).
        expect = [(s.counter, s.v_q15, s.i_q15) for s in parse_l0_stream(stream)]
        assert expect == [(200, 1234, -4321), (201, 111, 222)], f"p1={p1}: {expect}"
        return stream, expect
    raise AssertionError(f"no two-A5 vector found for p1={p1}, p2={p2}")


@cocotb.test()
async def test_frame_fuzz_corrupt(dut):
    """Seeded corruption fuzz: frame_rx == parse_l0_stream, zero diffs.

    64 streams of ~320 bytes mixing valid frames, garbage, 1-3-bit flips,
    truncations and SOF runs (seed 42). Per stream: exact frame equality
    plus crc_err/resync counts vs the SOF-scan reference derived from
    parse_l0_stream windows (same single golden as the frames).
    """
    start_clock(dut)
    rng = random.Random(42)
    total_frames = total_rejects = 0
    for s in range(FUZZ_STREAMS):
        parts: list[bytes] = []
        while sum(len(p) for p in parts) < FUZZ_MIN_BYTES:
            r = rng.random()
            if r < 0.45:
                parts.append(
                    encode_frame(
                        rng.randrange(1 << 32),
                        rng.randrange(-32768, 32768),
                        rng.randrange(-32768, 32768),
                    )
                )
            elif r < 0.60:
                parts.append(rng.randbytes(rng.randrange(1, 21)))  # garbage
            elif r < 0.75:
                f = bytearray(
                    encode_frame(
                        rng.randrange(1 << 32),
                        rng.randrange(-32768, 32768),
                        rng.randrange(-32768, 32768),
                    )
                )
                for _ in range(rng.randrange(1, 4)):
                    f[rng.randrange(11)] ^= 1 << rng.randrange(8)
                parts.append(bytes(f))
            elif r < 0.85:
                parts.append(
                    encode_frame(
                        rng.randrange(1 << 32),
                        rng.randrange(-32768, 32768),
                        rng.randrange(-32768, 32768),
                    )[: rng.randrange(1, 11)]
                )
            else:
                parts.append(b"\xa5" * rng.randrange(1, 6))  # SOF run
        stream = b"".join(parts)
        await reset_frame_rx(dut)
        captured, errors = await run_stream(dut, stream)
        expect = [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(stream)]
        assert captured == expect, f"stream {s}: frame mismatch"
        rej, rsy = count_stream(stream)
        assert len(errors) == rej, f"stream {s}: crc_err {len(errors)} != ref {rej}"
        assert await read_counters(dut) == (rej, rsy), f"stream {s}: counter mismatch"
        total_frames += len(expect)
        total_rejects += rej
    cocotb.log.info(
        f"fuzz: {FUZZ_STREAMS} streams, {total_frames} frames, {total_rejects} rejects, zero diffs"
    )


@cocotb.test()
async def test_frame_flips88(dut):
    """All 88 single-bit flips of one frame through the RTL: all rejected.

    CRC16-CCITT-FALSE detects every single-bit error at this length; this
    pins that fact through frame_rx. Note the SOF byte is special: flipping
    a bit in it destroys the sync marker, so no candidate ever opens
    (crc_err == 0, golden agrees) — hence error counts come from the
    parse_l0_stream-derived reference, not a hardcoded 1. Every flip must
    still lose the base frame and match the golden exactly (no forged frame).
    """
    start_clock(dut)
    pre = encode_frame(76, 100, -100)
    base = encode_frame(77, 12345, -23456)
    post = encode_frame(78, -30000, 30000)
    for bit in range(88):
        bad = bytearray(base)
        bad[bit // 8] ^= 1 << (bit % 8)
        stream = pre + bytes(bad) + post
        await reset_frame_rx(dut)
        captured, errors = await run_stream(dut, stream)
        expect = [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(stream)]
        assert expect == [(76, 100, -100), (78, -30000, 30000)], (
            f"bit {bit}: golden shows an accidental pass {expect} — change the base frame constants"
        )
        assert captured == expect, f"bit {bit}: RTL mismatch {captured}"
        assert await read_counters(dut) == count_stream(stream), (
            f"bit {bit}: counter mismatch {await read_counters(dut)}"
        )
    cocotb.log.info("flips88: all 88 single-bit flips match golden through RTL")


@cocotb.test()
async def test_frame_a5_in_crc(dut):
    """Frames whose CRC bytes equal 0xA5 parse exactly (positional, not SOF)."""
    start_clock(dut)
    await reset_frame_rx(dut)
    v0, i0 = 2000, -3000
    los = [c for c in range(20000) if encode_frame(c, v0, i0)[9] == 0xA5][:3]
    his = [c for c in range(20000) if encode_frame(c, v0, i0)[10] == 0xA5][:3]
    assert len(los) == 3 and len(his) == 3, "no A5-in-CRC frames found"
    stream = b"".join(encode_frame(c, v0, i0) for c in los + his)
    captured, errors = await run_stream(dut, stream)
    assert errors == []
    assert captured == [(c, v0, i0) for c in los + his]
    assert captured == [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(stream)]
    assert await read_counters(dut) == (0, 0)


@cocotb.test()
async def test_frame_counters_saturate(dut):
    """Saturating counters: increment exact, then sticky at 0xFFFF.

    16-bit saturation is unreachable quickly, so it is proven naturally:
    simulator VPI writes (deposit AND force) demonstrably do not land on
    these Verilator flops (measured: readback stays 0x0000), so 70,000
    bad-CRC frames each carrying an inner A5 drive 141,063 rejects AND
    resyncs (reference-counted, ~2.15x margin over 0xFFFF), sticking both
    counters at 0xFFFF — a wrap would read back a small number. The long
    stream runs monitor-free at 1 byte/cycle (counters are the
    observation; frame-level exactness is covered by the other nodes).
    Increment behavior is pinned from reset first, against the reference.
    """
    start_clock(dut)
    bad = bytearray(encode_frame(90, 11, 22))
    bad[5] ^= 0xFF
    stream3 = bytes(bad) * 3
    # Phase A: increment exactness from reset (small stream, monitored).
    await reset_frame_rx(dut)
    captured, errors = await run_stream(dut, stream3)
    assert captured == [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(stream3)]
    assert await read_counters(dut) == count_stream(stream3)
    assert read_sig(dut.cnt_saturated, 1) == 0
    # Phase B: natural saturation of both counters together.
    await reset_frame_rx(dut)
    parts = []
    for i in range(70000):
        f = bytearray(encode_frame(1000 + i, 0x25A5, 0))
        f[9] ^= 0x01  # break CRC; V low byte 0xA5 rides every resync
        parts.append(bytes(f))
    stream = b"".join(parts)
    rej, rsy = count_stream(stream)
    assert rej > 0xFFFF and rsy > 0xFFFF, f"no saturation margin: {(rej, rsy)}"
    await run_stream_nomonitor(dut, stream)
    assert await read_counters(dut) == (0xFFFF, 0xFFFF)
    assert read_sig(dut.cnt_saturated, 1) == 1
    cocotb.log.info(f"counters: {rej} rejects / {rsy} resyncs saturate at 0xFFFF")
    # Phase C: record-close clear zeroes counters, flag stays sticky.
    # (No stream is fed on the saturated state: Phase B's trailing rescan
    # prefix would join it into one extra rejection — correct RTL
    # behavior, pinned by the fuzz instead. Counting-from-zero is proven
    # post-reset below; the flag flop is orthogonal to the counters.)
    dut.cnt_clear.value = 1
    await RisingEdge(dut.clk)
    await settle()
    dut.cnt_clear.value = 0
    await RisingEdge(dut.clk)
    await settle()
    assert await read_counters(dut) == (0, 0)
    assert read_sig(dut.cnt_saturated, 1) == 1, "flag must survive clear"
    # Fresh counting exactness + reset clears everything.
    await reset_frame_rx(dut)
    await settle()
    captured, errors = await run_stream(dut, stream3)
    assert captured == [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(stream3)]
    assert await read_counters(dut) == count_stream(stream3)
    assert read_sig(dut.cnt_saturated, 1) == 0
    cocotb.log.info("clear: counters zeroed, flag sticky, reset clears all")


@cocotb.test()
async def test_frame_clear_same_cycle(dut):
    """Same-cycle clear+rejection: increment wins (clear zeroes first).

    A bad-CRC completion lands on the edge where cnt_clear is high: the
    counter must read 1 afterwards (clear-wins would read 0). Resync
    count follows the parse-derived reference as usual.
    """
    start_clock(dut)
    await reset_frame_rx(dut)
    await settle()
    bad = bytearray(encode_frame(11, 333, -444))
    bad[9] ^= 0xFF  # corrupt CRC low byte
    stream = bytes(bad)
    for byte in stream[:10]:
        dut.data_in.value = byte
        dut.data_valid.value = 1
        await RisingEdge(dut.clk)
        await settle()
        dut.data_valid.value = 0
        await RisingEdge(dut.clk)
        await settle()
    dut.cnt_clear.value = 1
    dut.data_in.value = stream[10]
    dut.data_valid.value = 1
    await RisingEdge(dut.clk)  # completion edge with clear high
    await settle()
    dut.data_valid.value = 0
    dut.cnt_clear.value = 0
    await RisingEdge(dut.clk)
    await settle()
    assert await read_counters(dut) == count_stream(stream)
    assert (await read_counters(dut))[0] == 1, "increment must win over clear"
    cocotb.log.info("clear-same-cycle: rejection counted from zero")


@cocotb.test()
async def test_frame_reset_midframe(dut):
    """Reset mid-candidate: silence under reset, exact parsing after.

    Bytes driven while rst_n is low are ignored (no frame_valid/crc_err,
    counters stay zero); the post-reset stream parses byte-exact with
    counters reflecting post-reset events only.
    """
    start_clock(dut)
    await reset_frame_rx(dut)
    f0 = encode_frame(50, 111, 222)
    bad = bytearray(encode_frame(52, 555, 666))
    bad[5] ^= 0xFF
    tail = bytes(bad) + encode_frame(53, 777, -888)
    captured, errors, stop_ev = [], [], Event()
    mon = cocotb.start_soon(monitor(dut, captured, errors, stop_ev))
    await feed_bytes(dut, f0[:6])  # partial candidate, then reset drops it
    dut.rst_n.value = 0
    await feed_bytes(dut, f0[6:] + encode_frame(51, 333, -444))  # under reset: silent
    assert (len(captured), len(errors)) == (0, 0), f"output under reset: {captured}"
    assert await read_counters(dut) == (0, 0)
    assert read_sig(dut.cnt_saturated, 1) == 0
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await settle()
    await feed_bytes(dut, tail)
    for _ in range(5):
        await RisingEdge(dut.clk)
    await settle()
    stop_ev.set()
    await mon
    expect = [(x.counter, x.v_q15, x.i_q15) for x in parse_l0_stream(tail)]
    assert captured == expect == [(53, 777, -888)]
    assert len(errors) == 1
    assert await read_counters(dut) == count_stream(tail)
    assert read_sig(dut.cnt_saturated, 1) == 0
