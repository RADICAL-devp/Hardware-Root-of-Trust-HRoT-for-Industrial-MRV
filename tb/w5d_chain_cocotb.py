"""Week 5d1 cocotb: chain_ctrl hash-chain sequencer + win_counter.

chain_ctrl holds NO hash engine: it sequences ONE external sha256_wrap
(same sharing as hmac_core; tb/chain_wrap_pair.v wires them, top.v owns
the mux in 5d2). Per record: latch prev (genesis zeros iff
prev_sel_genesis, else the held digest) -> stream 32 prev bytes ->
stream sample bytes (8 B atomic units, ready-gated) -> latch the 36 B
descriptor -> stream it -> wait the digest -> latch window_hash (which
becomes the next record's prev) -> done pulse.

Phase contract (frozen here; RTL must match): 0 IDLE, 1 INIT, 2 PREV,
3 SAMP, 4 SAMP_END (lone-last seal of an empty tail), 5 DESC_WAIT,
6 DESC_FEED, 7 DV, 8 FINISH. start_in while busy is IGNORED (same
protocol as hmac_core). sample_valid while not ready and wrap overflow
outside init are sticky errors (abort, no done).

Oracle: hashlib (independent of the DUT) over prev || samples ||
descriptor; the tomb test additionally cross-checks
edge/attestation.build_record. Hashed bytes are exactly the in-window
frame_rx-ACCEPTED bytes (amendment 4): CRC-rejected L0 frames never
enter — the TB models rejection by withholding bytes, and the digest
must equal hashlib over the accepted-only stream.

win_counter (standalone top): incr_in + close_valid_in sampled together;
advances ONLY on valid closes; a rejected close (valid=0) never
advances (M-C3). Halt with STICKY flag at 2^32-1, never wraps (M-C4;
wrap would reuse nonces). CNT_INIT is test-only (VPI writes do not land
on Verilator flops); production/top.v/Yosys use the default 0.
"""

import hashlib
import os
import random
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge

from tb.w4b_common import read_sig, reset_dut, settle, start_clock

REPO = Path(__file__).resolve().parents[1]

PH_DESC_WAIT = 5


def meas_open(name: str):
    """Durable measurement file (sim print()s vanish on pass; files don't)."""
    sim = os.environ.get("WEEK4B_TAG", "unknown")
    return open(REPO / "results" / f"meas_{name}_{sim}.txt", "w")


def cyc_now() -> int:
    """Current cycle index [counts] (10 ns clock)."""
    return int(cocotb.utils.get_sim_time("ns") // 10)


def desc_word(desc: bytes) -> int:
    """Pack 36 descriptor bytes LSB-first into the parallel port [int]."""
    assert len(desc) == 36
    return int.from_bytes(desc, "little")


async def start_monitor(dut, rec: dict):
    """Background sampler: done/error/busy/phase + wrap taps."""
    while not rec["stop"]:
        await RisingEdge(dut.clk)
        await settle()
        cyc = cyc_now()
        if read_sig(dut.done_out, 1):
            rec["done_at"].append(cyc)
        if read_sig(dut.error_out, 1) and rec["err_at"] is None:
            rec["err_at"] = cyc
        if read_sig(dut.tap_accept, 1):
            rec["accepts"].append((read_sig(dut.phase_out, 4), cyc))
        if read_sig(dut.tap_winit, 1) and not rec["winit_prev"]:
            rec["winits"].append(cyc)
        rec["winit_prev"] = bool(read_sig(dut.tap_winit, 1))
        if read_sig(dut.tap_dv, 1) and not rec["dv_prev"]:
            rec["dvedges"].append(cyc)
        rec["dv_prev"] = bool(read_sig(dut.tap_dv, 1))
        if read_sig(dut.tap_wdv, 1):
            rec["wdv_at"].append(cyc)
        if read_sig(dut.tap_ovf, 1) and rec["ovf_at"] is None:
            rec["ovf_at"] = cyc
        ph = read_sig(dut.phase_out, 4)
        if ph != rec["phase_prev"]:
            rec["phases"].append((ph, cyc))
            rec["phase_prev"] = ph
        rec["peak"] = max(rec["peak"], read_sig(dut.tap_occ, 9))


def new_rec() -> dict:
    """Fresh monitor record."""
    return {
        "stop": False,
        "done_at": [],
        "err_at": None,
        "accepts": [],
        "winits": [],
        "winit_prev": False,
        "dvedges": [],
        "dv_prev": False,
        "wdv_at": [],
        "ovf_at": None,
        "phases": [],
        "phase_prev": 0,
        "peak": 0,
    }


async def quiesce_chain(dut) -> None:
    """Drive every chain-harness input to idle."""
    dut.start_in.value = 0
    dut.prev_sel_genesis.value = 0
    dut.sample_data_in.value = 0
    dut.sample_valid_in.value = 0
    dut.sample_last_in.value = 0
    dut.desc_data_in.value = 0
    dut.desc_valid_in.value = 0


async def pulse_start(dut, genesis: int, cycles: int = 2) -> None:
    """Open one chain record (sampled in IDLE; ignored when busy)."""
    dut.prev_sel_genesis.value = genesis
    dut.start_in.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    await settle()
    dut.start_in.value = 0


async def stream_samples(dut, data: bytes) -> None:
    """Stream sample bytes honoring sample_ready_out (valid-hold handshake)."""
    n = len(data)
    idx = 0
    dut.sample_valid_in.value = 0
    dut.sample_last_in.value = 0
    if n == 0:
        while not read_sig(dut.sample_ready_out, 1):
            await RisingEdge(dut.clk)
            await settle()
        dut.sample_last_in.value = 1  # lone last seals the empty tail
        await RisingEdge(dut.clk)
        await settle()
        dut.sample_last_in.value = 0
        return
    while idx < n:
        if not read_sig(dut.sample_ready_out, 1):
            dut.sample_valid_in.value = 0
            dut.sample_last_in.value = 0
            await RisingEdge(dut.clk)
            await settle()
            continue
        dut.sample_data_in.value = data[idx]
        dut.sample_valid_in.value = 1
        dut.sample_last_in.value = 1 if idx == n - 1 else 0
        await RisingEdge(dut.clk)
        await settle()
        idx += 1
    dut.sample_valid_in.value = 0
    dut.sample_last_in.value = 0


async def wait_phase(dut, phase: int, limit: int = 10_000) -> None:
    """Wait until phase_out reads `phase` (DESC-wait handshake)."""
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.phase_out, 4) == phase:
            return
    raise AssertionError(f"phase {phase} never reached (stuck)")


async def wait_done(dut, rec: dict, limit: int) -> int:
    """Wait for a done pulse past the recorded ones; return cycle [counts]."""
    seen = len(rec["done_at"])
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await settle()
        if len(rec["done_at"]) > seen:
            return rec["done_at"][-1]
    raise AssertionError(
        f"no chain done within {limit} cycles "
        f"(err_at={rec['err_at']} accepts={len(rec['accepts'])} "
        f"busy={read_sig(dut.busy_out, 1)} phase={read_sig(dut.phase_out, 4)})"
    )


async def run_record(
    dut, prev: bytes, samples: bytes, desc: bytes, genesis: bool
) -> tuple[bytes, dict]:
    """One clean chain record: start, stream, latch desc, await done.

    Returns (window_hash [bytes], monitor record). Tag must equal
    hashlib over prev || samples || desc; the DUT's prev latch must
    then read back the same digest (chain linkage).
    """
    assert len(prev) == 32 and len(desc) == 36
    assert not read_sig(dut.busy_out, 1), "chain busy at record start"
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_start(dut, 1 if genesis else 0)
    t_start = cyc_now()  # job clock starts at stream (past TB settle)
    await stream_samples(dut, samples)
    await wait_phase(dut, PH_DESC_WAIT)
    dut.desc_data_in.value = desc_word(desc)
    dut.desc_valid_in.value = 1
    await RisingEdge(dut.clk)
    await settle()
    dut.desc_valid_in.value = 0
    nblocks = (len(prev) + len(samples) + len(desc) + 9 + 63) // 64
    if nblocks == 0:
        nblocks = 1
    done_cyc = await wait_done(dut, rec, 10 * (len(samples) + nblocks * 100 + 1000))
    rec["job_blocks"] = nblocks
    rec["job_cycles"] = done_cyc - t_start
    # Universal final tail (last-accept -> done): 66 dv + drain + sample +
    # done edge. The post-last-accept path is TB-free (no more stream
    # bytes), so this holds for EVERY clean job at ANY length — a stall
    # delays accepts (caught by gap/total pins), never the tail.
    assert done_cyc - rec["accepts"][-1][1] == 69, (
        f"final tail {done_cyc - rec['accepts'][-1][1]} != 69",
        rec["accepts"],
    )
    tag = read_sig(dut.window_hash_out, 256).to_bytes(32, "big")
    prev_out = read_sig(dut.prev_hash_out, 256).to_bytes(32, "big")
    assert rec["err_at"] is None, "clean record raised error"
    assert rec["ovf_at"] is None, "clean record overflowed the wrap"
    assert prev_out == tag, "prev latch != window hash (linkage broken)"
    assert tag == hashlib.sha256(prev + samples + desc).digest(), "record digest != hashlib"
    rec["stop"] = True
    await mon
    await quiesce_chain(dut)
    return tag, rec


async def assert_core_bound(rec: dict, nblocks: int, tag: str) -> None:
    """Block structure pin: nblocks accepts, every spacing exactly 67.

    Single-message streaming at 1 B/cycle outruns the core drain, so no
    starvation gap may appear (small records only; multi-KB messages can
    stall on backpressure and must NOT use this pin).
    """
    cycs = [c for _, c in rec["accepts"]]
    assert len(cycs) == nblocks, f"{tag}: {len(cycs)} accepts != {nblocks} blocks"
    gaps = [b - a for a, b in zip(cycs, cycs[1:])]
    assert all(g == 67 for g in gaps), f"{tag}: spacings {sorted(set(gaps))} != [67]"


# Pinned job cycles (stream-start to done, TB convention) per record shape.
# Model: stream bytes (1 B/cyc) + blocks x 67 + first-accept latency +
# 68-cycle final tail (66 dv + drain + sample, measured on all records)
# + 2-edge descriptor handshake. Deterministic (rerun-identical); the pin
# guards any timing change in chain_ctrl/sha256_wrap/TB pacing.
CHAIN_CYCLES = {(3, True): 205, (5, False): 203, (2, False): 205}


@cocotb.test()
async def test_chain_genesis_link(dut):
    """3-record chain: genesis zeros, linkage exact, digests == hashlib."""
    start_clock(dut)
    await quiesce_chain(dut)
    await reset_dut(dut)
    rng = random.Random(5101)
    prev = bytes(32)
    mf = meas_open("chain_genesis_link")
    for k, n in enumerate((3, 5, 2)):
        samples = bytes(rng.randrange(256) for _ in range(8 * n))
        desc = bytes(rng.randrange(256) for _ in range(36))
        tag, rec = await run_record(dut, prev, samples, desc, genesis=(k == 0))
        assert tag == hashlib.sha256(prev + samples + desc).digest()
        assert_core_bound(rec, 2, f"rec{k}")
        assert rec["job_cycles"] == CHAIN_CYCLES[(n, k == 0)], (k, rec["job_cycles"])
        mf.write(
            f"rec{k} samples={8 * n} blocks={rec['job_blocks']} job_cycles={rec['job_cycles']}\n"
        )
        mf.write(f"  accepts={[c for _, c in rec['accepts']]}\n")
        mf.write(f"  phases={rec['phases']}\n")
        mf.flush()
        prev = tag  # chain linkage: next prev IS this digest
    mf.close()


@cocotb.test()
async def test_chain_tomb_link(dut):
    """Tombstoned-slot record chains its as-sent bytes (== build_record hash)."""
    import numpy as np

    from edge.attestation import SubWindow, build_record
    from sim.provision import derive_keys

    start_clock(dut)
    await quiesce_chain(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    n = 200
    subs = []
    for k in range(5):
        valid = k != 2  # slot 2: as-sent on the wire, then tombstoned
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
    msg = bytes(32)
    for s in subs:
        v = np.asarray(s.v_q15, dtype=np.int64).astype("<i2").tobytes()
        i = np.asarray(s.i_q15, dtype=np.int64).astype("<i2").tobytes()
        vi = np.asarray(s.v_q15, dtype=np.int64) * np.asarray(s.i_q15, dtype=np.int64)
        p = vi.astype("<i4").tobytes()
        msg += v + i + p
    desc = bytes.fromhex(record["descriptor_hex"])
    msg += desc
    assert hashlib.sha256(msg).hexdigest() == record["window_hash_hex"]  # layout pin
    tag, rec = await run_record(dut, bytes(32), msg[32:-36], desc, genesis=True)
    assert tag.hex() == record["window_hash_hex"]
    # 8068 B over 127 blocks: feed (1 B/cyc) outruns the 64 B/67 cyc
    # drain, so the FIFO never empties mid-message and no starvation gap
    # may appear — a stall here is a BUG, pinned twice (gaps + total).
    assert_core_bound(rec, 127, "tomb-8068B")
    assert rec["job_cycles"] == 8578, rec["job_cycles"]  # 67 + 126x67 + 69


@cocotb.test()
async def test_chain_rejected_frame(dut):
    """A CRC-rejected L0 frame's bytes never enter: digest over accepted-only.

    Amendment-4 model: frame_rx drops CRC-bad frames, so the hash stream
    is the accepted-byte subsequence. The TB withholds one 11-byte
    frame's worth mid-stream; the digest must equal hashlib over the
    accepted bytes only (a drop that changed the digest would be a
    binding hole; an accepted-byte change the digest misses would be
    worse — both directions pinned here).
    """
    start_clock(dut)
    await quiesce_chain(dut)
    await reset_dut(dut)
    rng = random.Random(5104)
    # Record A (genesis) so record B exercises the chained-prev path.
    prev_a = bytes(32)
    samples_a = bytes(rng.randrange(256) for _ in range(8 * 4))
    desc_a = bytes(rng.randrange(256) for _ in range(36))
    tag_a, _ = await run_record(dut, prev_a, samples_a, desc_a, genesis=True)
    assert tag_a == hashlib.sha256(prev_a + samples_a + desc_a).digest()
    # Record B: one 11-byte L0 frame withheld (CRC-reject model) mid-stream.
    full = bytes(rng.randrange(256) for _ in range(8 * 24))
    hole_at = 8 * 7  # drop bytes [hole_at, hole_at+11): one L0 frame
    accepted = full[:hole_at] + full[hole_at + 11 :]
    desc = bytes(rng.randrange(256) for _ in range(36))
    tag, rec = await run_record(dut, tag_a, accepted, desc, genesis=False)
    assert tag != hashlib.sha256(tag_a + full + desc).digest(), "hole changed nothing?"
    assert_core_bound(rec, 5, "rejected-frame")  # 249 B, no stall possible
    assert rec["job_cycles"] == 404, rec["job_cycles"]  # pinned (model above)
    with meas_open("chain_rejected_frame") as mf:
        mf.write(f"job_cycles={rec['job_cycles']} blocks={rec['job_blocks']}\n")


@cocotb.test()
async def test_chain_empty_samples(dut):
    """Degenerate 0-sample record (lone-last seal) digests prev || desc."""
    start_clock(dut)
    await quiesce_chain(dut)
    await reset_dut(dut)
    rng = random.Random(5105)
    prev = bytes(32)  # genesis: DUT feeds zeros (sel=0 is covered in genesis_link)
    desc = bytes(rng.randrange(256) for _ in range(36))
    tag, rec = await run_record(dut, prev, b"", desc, genesis=True)
    assert tag == hashlib.sha256(prev + desc).digest()
    assert rec["ovf_at"] is None
    assert_core_bound(rec, 2, "empty-samples")  # 68 B
    assert rec["job_cycles"] == 206, rec["job_cycles"]  # pinned (model above)
    with meas_open("chain_empty_samples") as mf:
        mf.write(f"job_cycles={rec['job_cycles']} blocks={rec['job_blocks']}\n")


@cocotb.test()
async def test_chain_stream_violation(dut):
    """Bytes forced while not ready: sticky error, no done, clean job after."""
    start_clock(dut)
    await quiesce_chain(dut)
    await reset_dut(dut)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    await pulse_start(dut, 1)
    # PREV phase streams prev bytes internally: sample_ready reads LOW —
    # a producer that forces valid anyway must trip the error, not corrupt.
    dut.sample_data_in.value = 0xAA
    dut.sample_valid_in.value = 1
    for _ in range(10):
        await RisingEdge(dut.clk)
        await settle()
    dut.sample_valid_in.value = 0
    assert rec["err_at"] is not None, "forced bytes while not ready set no error"
    assert not rec["done_at"], "aborted record completed a digest"
    assert not read_sig(dut.busy_out, 1), "aborted record left busy set"
    rec["stop"] = True
    await mon
    rng = random.Random(5106)
    prev = bytes(32)
    samples = bytes(rng.randrange(256) for _ in range(64))
    desc = bytes(rng.randrange(256) for _ in range(36))
    tag, rec2 = await run_record(dut, prev, samples, desc, genesis=True)
    assert tag == hashlib.sha256(prev + samples + desc).digest()
    assert rec2["err_at"] is None and rec2["ovf_at"] is None


async def counter_step(dut, incr: int, valid: int) -> int:
    """Drive incr/valid for one edge; return the sampled count [u32]."""
    dut.incr_in.value = incr
    dut.close_valid_in.value = valid
    await RisingEdge(dut.clk)
    await settle()
    count = read_sig(dut.count_out, 32)
    dut.incr_in.value = 0
    dut.close_valid_in.value = 0
    return count


@cocotb.test()
async def test_counter_mono(dut):
    """1000 valid closes count exactly; idle/rejected never advance (M-C3)."""
    start_clock(dut)
    dut.incr_in.value = 0
    dut.close_valid_in.value = 0
    await reset_dut(dut)
    assert read_sig(dut.count_out, 32) == 0, "counter reset value != INIT"
    assert read_sig(dut.halted_out, 1) == 0
    for k in range(1, 1001):
        assert await counter_step(dut, 1, 1) == k, f"count diverged at {k}"
    for _ in range(10):  # idle edges: no advance without a close
        assert await counter_step(dut, 0, 0) == 1000
    for _ in range(5):  # REJECTED closes (valid=0): must not advance
        assert await counter_step(dut, 1, 0) == 1000, "rejected close advanced (M-C3)"
    assert read_sig(dut.halted_out, 1) == 0
    assert await counter_step(dut, 1, 1) == 1001


@cocotb.test()
async def test_counter_halt(dut):
    """CNT_INIT near max: saturate at 2^32-1 with sticky halt, never wrap (M-C4)."""
    start_clock(dut)
    dut.incr_in.value = 0
    dut.close_valid_in.value = 0
    await reset_dut(dut)
    max_u32 = (1 << 32) - 1
    assert read_sig(dut.count_out, 32) == max_u32 - 2, "CNT_INIT not applied"
    assert await counter_step(dut, 1, 1) == max_u32 - 1
    assert read_sig(dut.halted_out, 1) == 0, "halt set before reaching max"
    assert await counter_step(dut, 1, 1) == max_u32
    assert read_sig(dut.halted_out, 1) == 0, "halt set on arrival (only on excess)"
    assert await counter_step(dut, 1, 1) == max_u32, "counter wrapped past max (M-C4)"
    assert read_sig(dut.halted_out, 1) == 1, "halt flag not sticky"
    for _ in range(3):
        assert await counter_step(dut, 1, 1) == max_u32
    assert read_sig(dut.halted_out, 1) == 1


@cocotb.test()
async def test_counter_reset(dut):
    """Reset returns to INIT (volatile): documents the HW NV-counter note."""
    start_clock(dut)
    dut.incr_in.value = 0
    dut.close_valid_in.value = 0
    await reset_dut(dut)
    for k in range(1, 8):
        assert await counter_step(dut, 1, 1) == k
    await reset_dut(dut)
    assert read_sig(dut.count_out, 32) == 0, "reset did not return to INIT"
    assert read_sig(dut.halted_out, 1) == 0, "reset did not clear halt"
    assert await counter_step(dut, 1, 1) == 1
