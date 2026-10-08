"""Week 5c cocotb: sha256_wrap streaming SHA-256 engine (toplevel=sha256_wrap).

The wrap owns one secworks sha256_core (66 cycles/block, no padding) and
adds: a 256-byte input FIFO with REAL backpressure (ready_out =
FILL && not-full), 0x80/length padding, init-abort, a sticky overflow
flag (valid_in && !ready_out, never silent), digest latch + 1-cycle
digest_valid pulse, and an occupancy tap for peak reporting.

Driving convention (matches the RTL contract): inputs REGISTERED — set
before a rising edge, sampled after (+1 ns settle, tb.w4b_common).
Protocol: pulse init_in (2 cycles, aborts anything) -> stream bytes with
valid_in honoring ready_out -> last_in WITH the final byte (or lone
last_in for the empty message) -> wait digest_valid_out. After the seal
ready_out reads LOW (message sealed, digest pending); bytes presented
then set overflow and are IGNORED (the in-flight digest stays exact).
The 5d chain feed models this with a skid buffer (withholds while not
ready); this TB withholds in Python for the overlap case and forces
bytes for the violation cases.

Oracle: hashlib (independent of the DUT). Fixed KAT below comes from an
actual hashlib run (item 3); the old descriptor-first order gives a
different digest (checked at generation).
"""

import hashlib
import os
import random
import struct
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge

from tb.w4b_common import CLK_NS, read_sig, reset_dut, settle, start_clock

REPO = Path(__file__).resolve().parents[1]


def meas_open(name: str):
    """Durable measurement file (sim print()s vanish on pass; files don't)."""
    sim = os.environ.get("WEEK4B_TAG", "unknown")
    return open(REPO / "results" / f"meas_{name}_{sim}.txt", "w")


KAT_HEX = "9c239a053291e5e92ce168a5592ac293e31b0303729009978d67dfa521b2cad1"


def kat_message() -> bytes:
    """Fixed 92-byte KAT: prev=bytes(range(32)), 3 samples, desc=range(36)."""
    prev = bytes(range(32))
    v = struct.pack("<3h", 1, -2, 3)
    i = struct.pack("<3h", 4, 5, -6)
    p = struct.pack("<3i", 4, -10, -18)
    return prev + v + i + p + bytes(range(36))


def window_message(seed: int, n_samples: int) -> bytes:
    """Deterministic prev || samples || descriptor byte string [bytes]."""
    rng = random.Random(seed)
    prev = bytes(rng.randrange(256) for _ in range(32))
    samples = bytes(rng.randrange(256) for _ in range(8 * n_samples))
    desc = bytes(rng.randrange(256) for _ in range(36))
    return prev + samples + desc


def pattern_message(seed: int, length: int) -> bytes:
    """Deterministic non-zero pattern [bytes] (seeded, reproducible)."""
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(length))


def cyc_now() -> int:
    """Current cycle index [counts] (10 ns clock)."""
    return int(cocotb.utils.get_sim_time("ns") // CLK_NS)


async def idle_inputs(dut) -> None:
    """Quiesce wrap inputs (before reset and between messages)."""
    dut.data_in.value = 0
    dut.valid_in.value = 0
    dut.init_in.value = 0
    dut.last_in.value = 0


async def start_monitor(dut, rec: dict):
    """Background sampler: occupancy peak, overflow/ready/digest/core events."""
    while not rec["stop"]:
        await RisingEdge(dut.clk)
        await settle()
        cyc = cyc_now()
        occ = read_sig(dut.occupancy_out, 9)
        rec["peak"] = max(rec["peak"], occ)
        if read_sig(dut.overflow_out, 1) and rec["ovf_at"] is None:
            rec["ovf_at"] = cyc
        if not read_sig(dut.ready_out, 1):
            rec["ready_low"] = True
        if read_sig(dut.digest_valid_out, 1):
            rec["dv_at"].append(cyc)
        if read_sig(dut.core_init, 1) or read_sig(dut.core_next, 1):
            rec["accepts"].append(cyc)
        if read_sig(dut.core_digest_valid, 1) and not rec["dv_prev"]:
            rec["dvedges"].append(cyc)
        rec["dv_prev"] = bool(read_sig(dut.core_digest_valid, 1))
        # Internal snapshot (hang forensics; also cross-checks the TB-side
        # level counts against the DUT edge counters).
        rec["dbg"] = (
            read_sig(dut.accepts, 32),
            read_sig(dut.dones, 32),
            read_sig(dut.state, 2),
            read_sig(dut.pad_st, 2),
            read_sig(dut.block_avail, 1),
            read_sig(dut.fill, 6),
        )


def new_rec() -> dict:
    """Fresh monitor record."""
    return {
        "stop": False,
        "peak": 0,
        "ovf_at": None,
        "ready_low": False,
        "dv_at": [],
        "accepts": [],
        "dvedges": [],  # core digest RISES (edges, not sticky levels)
        "dv_prev": False,
        "dbg": None,  # last (accepts, dones, state, pad_st, block_avail, fill)
    }


async def pulse_init(dut, cycles: int = 2) -> None:
    """Abort anything, open a new message (init held `cycles`)."""
    dut.init_in.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    await settle()
    dut.init_in.value = 0


async def feed_honoring(dut, data: bytes, gap: int = 0, max_bytes: int | None = None) -> int:
    """Stream bytes honoring ready_out; last rides the final byte.

    Returns accepted bytes [counts]. Stops after `max_bytes` WITHOUT
    sealing (abort-test helper); otherwise seals (lone last if empty).
    """
    n = len(data) if max_bytes is None else min(len(data), max_bytes)
    for idx in range(n):
        while True:
            await RisingEdge(dut.clk)
            await settle()
            if read_sig(dut.ready_out, 1):
                break
        dut.data_in.value = data[idx]
        dut.valid_in.value = 1
        dut.last_in.value = 1 if (max_bytes is None and idx == n - 1) else 0
        await RisingEdge(dut.clk)
        await settle()
        dut.valid_in.value = 0
        dut.last_in.value = 0
        for _ in range(gap):
            await RisingEdge(dut.clk)
            await settle()
    if n == 0 and max_bytes is None:
        dut.last_in.value = 1  # lone last seals the empty message
        await RisingEdge(dut.clk)
        await settle()
        dut.last_in.value = 0
    return n


async def feed_fast(dut, data: bytes) -> int:
    """Stream at 1 B/cycle while ready holds, honoring backpressure.

    Between-byte gap is zero when flowing: after a consume edge with
    ready still high the next byte presents immediately (valid stays
    high). On ready-low valid drops the same settle (the in-flight byte
    WAS consumed — ready fell because the FIFO filled — so no overflow).
    valid is high only with ready high at the consuming edge: overflow
    never sets. Returns accepted bytes [counts]; seals with last.
    """
    n = len(data)
    idx = 0
    dut.valid_in.value = 0
    dut.last_in.value = 0
    while idx < n:
        if not read_sig(dut.ready_out, 1):
            dut.valid_in.value = 0
            dut.last_in.value = 0
            await RisingEdge(dut.clk)
            await settle()
            continue
        dut.data_in.value = data[idx]
        dut.valid_in.value = 1
        dut.last_in.value = 1 if idx == n - 1 else 0
        await RisingEdge(dut.clk)
        await settle()
        idx += 1
    dut.valid_in.value = 0
    dut.last_in.value = 0
    if n == 0:
        dut.last_in.value = 1  # lone last seals the empty message
        await RisingEdge(dut.clk)
        await settle()
        dut.last_in.value = 0
    return n


async def wait_digest(dut, rec: dict, limit: int, since: int | None = None) -> int:
    """Wait for a digest_valid past index `since`; return its cycle [counts].

    Default `since` = pulses already recorded (wait for a NEW one — right
    for back-to-back phases). Pass `since = 0` (or an earlier snapshot)
    when the pulse may have fired before polling started (overlap case).
    """
    seen = len(rec["dv_at"]) if since is None else since
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await settle()
        if len(rec["dv_at"]) > seen:
            return rec["dv_at"][-1]
    raise AssertionError(
        f"no digest_valid within {limit} cycles "
        f"(accepts={len(rec['accepts'])} dvedges={rec['dvedges']} "
        f"peak={rec['peak']} ovf_at={rec['ovf_at']} dbg={rec['dbg']})"
    )


async def run_message(dut, data: bytes, gap: int = 0, limit: int = 0) -> tuple[int, dict]:
    """Full message cycle: reset-less init, feed, seal, digest.

    Returns (digest [int], monitor record). `limit` bounds the digest
    wait [cycles]; default scales with the message (10x + margin).
    """
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_init(dut)
    await feed_honoring(dut, data, gap=gap)
    nblocks = 1 if not data else len(data) // 64 + (1 if len(data) % 64 <= 55 else 2)
    bound = limit or (10 * (len(data) + nblocks * 100 + 1000))
    await wait_digest(dut, rec, bound)
    digest = read_sig(dut.digest_out, 256)
    rec["stop"] = True
    await mon
    await idle_inputs(dut)
    return digest, rec


@cocotb.test()
async def test_hash_nist(dut):
    """NIST vectors + pad boundaries 55/56/63/64/119/120 vs hashlib."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    vectors = [
        b"",
        b"abc",
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",  # 448-bit
        b"abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmn"
        b"hijklmnopijklmnopqjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu",  # 896-bit
    ]
    for length in (55, 56, 63, 64, 119, 120):
        vectors.append(pattern_message(1000 + length, length))
    for idx, msg in enumerate(vectors):
        digest, rec = await run_message(dut, msg)
        assert digest == int(hashlib.sha256(msg).hexdigest(), 16), (
            f"vector {idx} len={len(msg)} got={digest:064x} exp={hashlib.sha256(msg).hexdigest()}"
        )
        assert rec["ovf_at"] is None, f"vector {idx}: unexpected overflow"
        assert len(rec["dv_at"]) == 1, f"vector {idx}: {len(rec['dv_at'])} digests"
    print(f"MEAS nist: blocks+cycles ok over {len(vectors)} vectors")


@cocotb.test()
async def test_hash_1M(dut):
    """1,000,000-bit ('a' x 125,000) NIST vector vs hashlib (slow-scale)."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    msg = b"a" * 125_000
    digest, rec = await run_message(dut, msg)
    assert digest == int(hashlib.sha256(msg).hexdigest(), 16)
    assert rec["ovf_at"] is None
    print(f"MEAS 1M: peak_occ={rec['peak']}")


@cocotb.test()
async def test_hash_window(dut):
    """Full-window 80,068-byte message (prev||samples||desc) vs hashlib."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    msg = window_message(seed=7, n_samples=10_000)
    assert len(msg) == 32 + 80_000 + 36 == 80_068
    digest, rec = await run_message(dut, msg)
    assert digest == int(hashlib.sha256(msg).hexdigest(), 16)
    assert rec["ovf_at"] is None
    print(f"MEAS window: len={len(msg)} peak_occ={rec['peak']}")


@cocotb.test()
async def test_hash_tomb(dut):
    """Item 1: tombstoned-slot record hashes its as-sent samples (Python layout)."""
    import numpy as np

    from edge.attestation import SubWindow, build_record
    from sim.provision import derive_keys

    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    n = 200
    subs = []
    for k in range(5):
        valid = k != 2  # slot 2: as-sent on the wire, then tombstoned in power_calc
        subs.append(
            SubWindow(
                v_q15=np.array([1000 + k] * n, dtype=np.int64),
                i_q15=np.array([500 - k] * n, dtype=np.int64),
                valid=valid,
            )
        )
    record, _ = build_record(
        counter=3,
        window_start=30000,
        sub_windows=subs,
        device_id=km.device_id,
        prev_hash=bytes(32),
        energy_prev_uwh=0,
        hmac_key=km.hmac_key,
    )
    assert record["zc_samples"][2] == 0  # tomb slot records M = 0 ...
    # ... but its as-sent sample bytes ride the hash stream (drop-oblivious).
    msg = bytes(32)
    for s in subs:
        v = np.asarray(s.v_q15, dtype=np.int64).astype("<i2").tobytes()
        i = np.asarray(s.i_q15, dtype=np.int64).astype("<i2").tobytes()
        vi = np.asarray(s.v_q15, dtype=np.int64) * np.asarray(s.i_q15, dtype=np.int64)
        p = vi.astype("<i4").tobytes()
        msg += v + i + p
    msg += bytes.fromhex(record["descriptor_hex"])
    assert hashlib.sha256(msg).hexdigest() == record["window_hash_hex"]  # layout pin
    clean = bytes(32) + b"\x00" * (8 * 5 * n) + bytes.fromhex(record["descriptor_hex"])
    assert len(msg) == len(clean)  # tombstone never changes the hashed length
    digest, rec = await run_message(dut, msg)
    assert digest == int(record["window_hash_hex"], 16)
    assert rec["ovf_at"] is None


@cocotb.test()
async def test_hash_lengths(dut):
    """8,191 / 8,192 / 8,193-byte lengths vs hashlib (multi-block seams)."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    for length in (8191, 8192, 8193):
        msg = pattern_message(2000 + length, length)
        digest, rec = await run_message(dut, msg)
        assert digest == int(hashlib.sha256(msg).hexdigest(), 16), f"len={length}"
        assert rec["ovf_at"] is None


@cocotb.test()
async def test_hash_backtoback(dut):
    """Back-to-back messages (empty -> 55 B -> abort mid-stream -> 119 B)."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    msg_b = pattern_message(31, 55)
    msg_d = pattern_message(32, 119)
    digest_a, rec_a = await run_message(dut, b"")
    assert digest_a == int(hashlib.sha256(b"").hexdigest(), 16)
    digest_b, _ = await run_message(dut, msg_b)
    assert digest_b == int(hashlib.sha256(msg_b).hexdigest(), 16)
    # Abandon a 5,000-byte message mid-FILL: no digest may ever complete.
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_init(dut)
    await feed_honoring(dut, pattern_message(33, 5_000), max_bytes=100)
    await pulse_init(dut)  # abort
    for _ in range(300):
        await RisingEdge(dut.clk)
        await settle()
    assert not rec["dv_at"], "abandoned message completed a digest"
    rec["stop"] = True
    await mon
    digest_d, rec_d = await run_message(dut, msg_d)
    assert digest_d == int(hashlib.sha256(msg_d).hexdigest(), 16)
    assert rec_d["ovf_at"] is None
    assert digest_a != digest_b != digest_d  # distinct messages, distinct digests


@cocotb.test()
async def test_hash_backpressure(dut):
    """Line/mid/stress feed rates: exact digests, no overflow; peak per rate."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    msg = pattern_message(77, 5_000)
    exp = int(hashlib.sha256(msg).hexdigest(), 16)
    peaks = {}
    for name, gap in (("line", 119), ("mid", 1)):
        digest, rec = await run_message(dut, msg, gap=gap)
        assert digest == exp, f"{name}: digest wrong under backpressure"
        assert rec["ovf_at"] is None, f"{name}: overflow while honoring ready"
        peaks[name] = rec["peak"]
    # Stress at true 1 B/cycle (12 MB/s > ~11.6 MB/s core drain): the FIFO
    # must fill (ready drops, producer pauses) without overflow.
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_init(dut)
    await feed_fast(dut, msg)
    await wait_digest(dut, rec, 200_000)
    assert read_sig(dut.digest_out, 256) == exp, "stress: digest wrong"
    assert rec["ovf_at"] is None, "stress: overflow while honoring ready"
    assert rec["ready_low"], "stress: ready never dropped (FIFO never filled?)"
    peaks["stress"] = rec["peak"]
    rec["stop"] = True
    await mon
    await idle_inputs(dut)
    print(f"MEAS backpressure: peaks={peaks}")
    acc = rec["accepts"]
    sp = [b - a for a, b in zip(acc, acc[1:])]
    adv = [d - a for a, d in zip(acc, rec["dvedges"][: len(acc)])]
    drain = rec["dv_at"][0] - rec["dvedges"][-1]
    print(f"MEAS timing: spacing min/max={min(sp)}/{max(sp)} a2dv={sorted(set(adv))} drain={drain}")
    with meas_open("hash_backpressure") as mf:
        mf.write(f"peaks={peaks} spacing={sorted(set(sp))} a2dv={sorted(set(adv))} drain={drain}\n")
    assert sorted(set(adv)) == [66], adv  # core block cost == secworks README
    assert min(sp) == max(sp) == 67, sp  # core-bound spacing: 66 + 1 re-arm
    assert drain == 1, drain  # registered completion: final dv + 1
    # Net FIFO growth +(67-64) = +3 B/block over 79 blocks; measured peak
    # 221 (startup/drain transient off the +237 gross). Deterministic RTL:
    # both sims must agree exactly (regression tripwire for any timing
    # change in the FIFO/assembler/handoff path).
    assert peaks["stress"] == 221, peaks
    assert peaks["line"] <= 8, peaks  # 80 kB/s line rate: FIFO nearly empty
    assert peaks["mid"] <= 64, peaks


@cocotb.test()
async def test_hash_overflow(dut):
    """Ignoring producer (valid held high): sticky overflow + recovery."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_init(dut)
    dut.valid_in.value = 1  # never honors ready_out from here on
    # Input 1 B/cycle outruns the core drain (~64 B per 68 cycles, net
    # +4 B/block): the 256-byte FIFO fills after ~64 blocks, ready drops,
    # and the held-high valid sets the sticky flag. 6,000 cycles clears
    # the ~4,400-cycle fill with margin.
    for k in range(6_000):
        dut.data_in.value = k & 0xFF
        await RisingEdge(dut.clk)
        await settle()
    dut.valid_in.value = 0
    assert rec["ovf_at"] is not None, "ignoring producer set no overflow flag"
    assert rec["ready_low"], "ready never dropped under ignoring producer"
    rec["stop"] = True
    await mon
    await idle_inputs(dut)
    msg = pattern_message(78, 100)  # init clears the flag; message runs clean
    digest, rec2 = await run_message(dut, msg)
    assert digest == int(hashlib.sha256(msg).hexdigest(), 16)
    assert rec2["ovf_at"] is None


@cocotb.test()
async def test_hash_sealed_force(dut):
    """Bytes forced during POST (sealed): overflow sets, in-flight digest exact."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    msg = pattern_message(79, 5_000)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_init(dut)
    await feed_honoring(dut, msg)  # sealed on the final byte; ready drops
    dut.valid_in.value = 1  # misbehaving producer ignores the sealed state
    dut.data_in.value = 0xAA
    for _ in range(100):
        await RisingEdge(dut.clk)
        await settle()
    dut.valid_in.value = 0
    await wait_digest(dut, rec, 100_000)
    digest = read_sig(dut.digest_out, 256)
    rec["stop"] = True
    await mon
    await idle_inputs(dut)
    assert rec["ovf_at"] is not None, "forced sealed bytes set no overflow"
    assert digest == int(hashlib.sha256(msg).hexdigest(), 16), "forced bytes corrupted the digest"


@cocotb.test()
async def test_hash_abort_restart(dut):
    """Directed abort/restart: the sticky-digest_valid invariant (item c).

    secworks digest_valid is STICKY-HIGH until the next accept; the wrap
    counts EDGES. Re-init clears the edge detector, so an abort MUST also
    reset the core — otherwise the stale HIGH flag raises a PHANTOM edge
    and the restarted message completes early with a wrong digest (mutant
    M-H6 mechanism). Proved here two ways: abort mid-FILL (core hashing)
    and abort inside the sticky window (block done, next not accepted).
    Both restarts must complete exact with dones == accepts (TB reads the
    DUT edge/block counters) and exactly one new wrap digest each.
    """
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    # A: 100 bytes, no seal (>= 1 block handed), then abort mid-FILL.
    await pulse_init(dut)
    await feed_honoring(dut, pattern_message(101, 300), max_bytes=100)
    assert len(rec["accepts"]) >= 1, "no block handed before abort"
    await pulse_init(dut)  # abort: core held in reset while init HIGH
    # B: restarted message completes exact, counters aligned, one digest.
    msg_b = pattern_message(102, 200)
    n_dv = len(rec["dv_at"])
    await pulse_init(dut)
    await feed_honoring(dut, msg_b)
    await wait_digest(dut, rec, 20_000)
    assert read_sig(dut.digest_out, 256) == int(hashlib.sha256(msg_b).hexdigest(), 16)
    assert len(rec["dv_at"]) == n_dv + 1, "abandoned/restarted message digested twice"
    assert read_sig(dut.dones, 32) == read_sig(dut.accepts, 32) == 4, "phantom edge?"
    assert rec["ovf_at"] is None
    # C: one block, then poll the STICKY digest flag (persists: race-free).
    await pulse_init(dut)
    await feed_honoring(dut, pattern_message(103, 200), max_bytes=64)
    for _ in range(500):
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.core_digest_valid, 1):
            break
    else:
        raise AssertionError("no sticky digest_valid after one block")
    await pulse_init(dut)  # abort inside the sticky window
    # D: restarted message completes exact, aligned, single digest.
    msg_d = pattern_message(104, 119)
    n_dv = len(rec["dv_at"])
    await pulse_init(dut)
    await feed_honoring(dut, msg_d)
    await wait_digest(dut, rec, 20_000)
    assert read_sig(dut.digest_out, 256) == int(hashlib.sha256(msg_d).hexdigest(), 16)
    assert len(rec["dv_at"]) == n_dv + 1
    assert read_sig(dut.dones, 32) == read_sig(dut.accepts, 32) == 2, "phantom edge?"
    assert rec["ovf_at"] is None
    rec["stop"] = True
    await mon
    await idle_inputs(dut)


@cocotb.test()
async def test_hash_kat(dut):
    """Item 3: fixed hex known-answer vector (prev||samples||desc order)."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    assert kat_message() == bytes(range(32)) + struct.pack("<3h", 1, -2, 3) + struct.pack(
        "<3h", 4, 5, -6
    ) + struct.pack("<3i", 4, -10, -18) + bytes(range(36))
    digest, rec = await run_message(dut, kat_message())
    assert f"{digest:064x}" == KAT_HEX, f"KAT mismatch: got={digest:064x} exp={KAT_HEX}"
    assert rec["ovf_at"] is None


@cocotb.test()
async def test_hash_overlap(dut):
    """Item 2 (wrap level): N+1 arrives during N's tail; HMAC-window withhold; exact."""
    start_clock(dut)
    await idle_inputs(dut)
    await reset_dut(dut)
    msg_n = pattern_message(91, 2_000)
    msg_p = pattern_message(92, 1_000)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_init(dut)
    await feed_honoring(dut, msg_n)  # seal N; tail (pad) blocks drain next
    # N+1 "arrives" while N's tail drains: the contract says ready_out reads
    # LOW once sealed, so a compliant producer withholds (5d skid model).
    for _ in range(50):
        await RisingEdge(dut.clk)
        await settle()
        if not read_sig(dut.ready_out, 1):
            break
    assert not read_sig(dut.ready_out, 1), "ready high after seal (expected LOW)"
    # since=0: N's pulse may fire any time after the seal (it does not wait
    # for us to start polling — a `seen`-skip here would miss it entirely).
    await wait_digest(dut, rec, 50_000, since=0)
    assert read_sig(dut.digest_out, 256) == int(hashlib.sha256(msg_n).hexdigest(), 16)
    n_pulses = len(rec["dv_at"])
    # HMAC window (~350 cycles derived; proven exactly at hmac level): N+1
    # keeps arriving but stays withheld the whole window.
    for _ in range(400):
        await RisingEdge(dut.clk)
        await settle()
    await pulse_init(dut)
    await feed_honoring(dut, msg_p)
    await wait_digest(dut, rec, 50_000)
    assert read_sig(dut.digest_out, 256) == int(hashlib.sha256(msg_p).hexdigest(), 16)
    assert rec["ovf_at"] is None, "withheld overlap set overflow"
    assert len(rec["dv_at"]) == n_pulses + 1
    assert rec["peak"] == 1, rec["peak"]  # line-paced overlap never fills
    assert len(rec["dvedges"]) == 48, len(rec["dvedges"])  # 32 N-blocks + 16 N+1
    rec["stop"] = True
    await mon
    await idle_inputs(dut)
    print(f"MEAS overlap: peak_occ={rec['peak']} core_dvedges={len(rec['dvedges'])}")
    with meas_open("hash_overlap") as mf:
        mf.write(f"peak_occ={rec['peak']} core_dvedges={len(rec['dvedges'])}\n")
