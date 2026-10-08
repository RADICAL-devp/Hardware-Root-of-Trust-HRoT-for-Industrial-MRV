"""Week 5c cocotb: hmac_core HMAC-SHA256 sequencer (toplevel=hmac_wrap_pair).

hmac_core holds NO hash engine: it sequences ONE external sha256_wrap
(one-core sharing; the pair harness wires them, top.v will own the mux
in 5d). Per job: latch key (+ optional pre-hash pass for keys > 64 B),
stream the message in (msg_* handshake), INNER = SHA(key^ipad || msg),
OUTER = SHA(key^opad || inner), done pulse + latched tag. Errors
(key_len > KEY_MAX, wrap overflow via the inject mux, both sticky to
start/rst) abort with no done.

Oracle: Python hmac (stdlib, independent of the DUT) AND the
RFC 4231-published HMAC-SHA-256 hex below (fetched from rfc-editor.org;
case 5 asserts the 128-bit truncation prefix).-length tripwires guard
against mistyped vectors (lengths double-recalled from the RFC).
"""

import hashlib
import hmac as hmac_mod
import os
import random
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge

from sim.provision import derive_keys
from tb.w4b_common import read_sig, reset_dut, settle, start_clock

REPO = Path(__file__).resolve().parents[1]


def meas_open(name: str):
    """Durable measurement file (sim print()s vanish on pass; files don't)."""
    sim = os.environ.get("WEEK4B_TAG", "unknown")
    return open(REPO / "results" / f"meas_{name}_{sim}.txt", "w")


KEY_MAX = 160

# (name, key, data, published HMAC-SHA-256 hex or 128-bit prefix, keylen, datalen)
RFC = [
    (
        "c1",
        bytes([0x0B] * 20),
        b"Hi There",
        "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
        20,
        8,
    ),
    (
        "c2",
        b"Jefe",
        b"what do ya want for nothing?",
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
        4,
        28,
    ),
    (
        "c3",
        bytes([0xAA] * 20),
        bytes([0xDD] * 50),
        "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe",
        20,
        50,
    ),
    (
        "c4",
        bytes(range(1, 26)),
        bytes([0xCD] * 50),
        "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b",
        25,
        50,
    ),
    ("c5", bytes([0x0C] * 20), b"Test With Truncation", "a3b6167473100ee06e0c796c2955552b", 20, 20),
    (
        "c6",
        bytes([0xAA] * 131),
        b"Test Using Larger Than Block-Size Key - Hash Key First",
        "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54",
        131,
        54,
    ),
    (
        "c7",
        bytes([0xAA] * 131),
        b"This is a test using a larger than block-size key and a larger than "
        b"block-size data. The key needs to be hashed before being used by the "
        b"HMAC algorithm.",
        "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2",
        131,
        152,
    ),
]


def cyc_now() -> int:
    """Current cycle index [counts] (10 ns clock)."""
    return int(cocotb.utils.get_sim_time("ns") // 10)


def key_word(key: bytes) -> int:
    """Pack key bytes LSB-first into the 160-byte parallel port [int]."""
    assert len(key) <= KEY_MAX
    return int.from_bytes(key.ljust(KEY_MAX, b"\x00"), "little")


async def start_monitor(dut, rec: dict):
    """Background sampler: hmac done/error/busy/phase + wrap taps."""
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
        if read_sig(dut.tap_ovf, 1) and rec["ovf_at"] is None:
            rec["ovf_at"] = cyc
        rec["peak"] = max(rec["peak"], read_sig(dut.tap_occ, 9))
        if not read_sig(dut.tap_ready, 1):
            rec["ready_low"] = True


def new_rec() -> dict:
    """Fresh monitor record."""
    return {
        "stop": False,
        "done_at": [],
        "err_at": None,
        "accepts": [],  # (phase, cycle) per wrap block accept
        "winits": [],  # wrap-init rises: pass boundaries (one per pass)
        "winit_prev": False,
        "dvedges": [],
        "dv_prev": False,
        "ovf_at": None,
        "peak": 0,
        "ready_low": False,
    }


async def quiesce(dut) -> None:
    """Drive every harness input to idle."""
    dut.key_data_in.value = 0
    dut.key_len_in.value = 0
    dut.msg_data_in.value = 0
    dut.msg_valid_in.value = 0
    dut.msg_last_in.value = 0
    dut.start_in.value = 0
    dut.inject_en.value = 0
    dut.inject_data.value = 0
    dut.inject_valid.value = 0
    dut.inject_last.value = 0
    dut.inject_init.value = 0


async def pulse_start(dut, cycles: int = 2) -> None:
    """Arm one HMAC job (sampled in IDLE; ignored when busy)."""
    dut.start_in.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    await settle()
    dut.start_in.value = 0


async def stream_msg(dut, msg: bytes) -> None:
    """Stream the message honoring msg_ready_out (valid-hold handshake)."""
    n = len(msg)
    idx = 0
    dut.msg_valid_in.value = 0
    dut.msg_last_in.value = 0
    if n == 0:
        # Lone last seals the empty message. Contract: never signal before
        # ready (the msg stage comes ~70 cycles after start: P_INIT + pads).
        while not read_sig(dut.msg_ready_out, 1):
            await RisingEdge(dut.clk)
            await settle()
        dut.msg_last_in.value = 1
        await RisingEdge(dut.clk)
        await settle()
        dut.msg_last_in.value = 0
        return
    while idx < n:
        if not read_sig(dut.msg_ready_out, 1):
            dut.msg_valid_in.value = 0
            dut.msg_last_in.value = 0
            await RisingEdge(dut.clk)
            await settle()
            continue
        dut.msg_data_in.value = msg[idx]
        dut.msg_valid_in.value = 1
        dut.msg_last_in.value = 1 if idx == n - 1 else 0
        await RisingEdge(dut.clk)
        await settle()
        idx += 1  # consumed: ready was high at the edge (stable pre-edge)
    dut.msg_valid_in.value = 0
    dut.msg_last_in.value = 0


async def wait_done(dut, rec: dict, limit: int) -> int:
    """Wait for a done pulse past the recorded ones; return cycle [counts]."""
    seen = len(rec["done_at"])
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await settle()
        if len(rec["done_at"]) > seen:
            return rec["done_at"][-1]
    raise AssertionError(
        f"no hmac done within {limit} cycles "
        f"(err_at={rec['err_at']} accepts={len(rec['accepts'])} "
        f"busy={read_sig(dut.busy_out, 1)} phase={read_sig(dut.phase_out, 4)})"
    )


def pass_buckets(rec: dict) -> list[int]:
    """Wrap-block accepts grouped by pass (split on wrap-init rises)."""
    buckets: list[int] = []
    init_idx = 0
    inits = rec["winits"]
    for _, cyc in rec["accepts"]:
        while init_idx + 1 < len(inits) and inits[init_idx + 1] <= cyc:
            init_idx += 1
            buckets.append(0)
        if len(buckets) <= init_idx:
            buckets.append(0)
        buckets[init_idx] += 1
    return buckets


def derived_blocks(key_len: int, msg_len: int) -> list[int]:
    """Expected wrap blocks per pass: [pre?, inner, outer] (FIPS layout)."""
    out = []
    if key_len > 64:
        out.append(-(-(key_len + 9) // 64))  # pre-hash the long key
    out.append(-(-(64 + msg_len + 9) // 64))  # inner: pad-block + msg
    out.append(-(-(64 + 32 + 9) // 64))  # outer: pad-block + inner digest
    return out


async def run_hmac(dut, key: bytes, msg: bytes) -> tuple[bytes, dict]:
    """One clean job: load key, start, stream msg, await done. Returns (tag, rec)."""
    assert not read_sig(dut.busy_out, 1), "hmac busy at job start"
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    dut.key_data_in.value = key_word(key)
    dut.key_len_in.value = len(key)
    await pulse_start(dut)
    for _ in range(3):  # wrap re-initialized: pre-init flag state is stale
        await RisingEdge(dut.clk)
        await settle()
    rec["ovf_at"] = None
    t_start = cyc_now()  # job clock starts at stream (past TB settle)
    await stream_msg(dut, msg)
    nblocks = sum(derived_blocks(len(key), len(msg)))
    done_cyc = await wait_done(dut, rec, 10 * (len(key) + len(msg) + nblocks * 100 + 1000))
    rec["job_cycles"] = done_cyc - t_start
    rec["job_blocks"] = nblocks
    tag = read_sig(dut.hmac_out, 256).to_bytes(32, "big")
    assert rec["err_at"] is None, "clean job raised error"
    assert rec["ovf_at"] is None, "clean job overflowed the wrap"
    assert pass_buckets(rec) == derived_blocks(len(key), len(msg)), (
        pass_buckets(rec),
        derived_blocks(len(key), len(msg)),
    )
    rec["stop"] = True
    await mon
    await quiesce(dut)
    return tag, rec


# Pinned HMAC job cycles (start-stream to done, both sims identical).
# Derived model: blocks x 67 + stream bytes + per-pass control (~10);
# the pin guards any timing change in hmac_core/sha256_wrap/TB pacing.
RFC_CYCLES = {"c1": 403, "c2": 403, "c3": 403, "c4": 403, "c5": 403, "c6": 673, "c7": 807}


@cocotb.test()
async def test_hmac_rfc17(dut):
    """RFC 4231 cases 1-7: RTL == stdlib oracle == published hex."""
    start_clock(dut)
    await quiesce(dut)
    await reset_dut(dut)
    mf = meas_open("hmac_rfc17")
    for name, key, data, published, keylen, datalen in RFC:
        assert len(key) == keylen and len(data) == datalen, f"{name}: vector typo"
        tag, rec = await run_hmac(dut, key, data)
        oracle = hmac_mod.new(key, data, hashlib.sha256).digest()
        assert tag == oracle, f"{name}: RTL != oracle"
        assert oracle.hex().startswith(published), f"{name}: oracle != RFC (vector typo?)"
        assert tag.hex().startswith(published), f"{name}: RTL != RFC published"
        assert rec["job_cycles"] == RFC_CYCLES[name], (name, rec["job_cycles"])
        mf.write(f"{name} job_cycles={rec['job_cycles']} blocks={rec['job_blocks']}\n")
        mf.flush()
        print(f"MEAS {name}: job_cycles={rec['job_cycles']} blocks={rec['job_blocks']}")
    mf.close()


@cocotb.test()
async def test_hmac_random200(dut):
    """200 seeded pairs (provision keys, msg 0..200 B) vs oracle."""
    start_clock(dut)
    await quiesce(dut)
    await reset_dut(dut)
    total = 0
    for seed in range(5):
        rng = random.Random(5000 + seed)
        for k in range(40):
            key = derive_keys(1000 + seed * 100 + k).hmac_key
            ln = 0 if k == 0 else 1 + rng.randrange(200)
            msg = bytes(rng.randrange(256) for _ in range(ln))
            tag, _ = await run_hmac(dut, key, msg)
            assert tag == hmac_mod.new(key, msg, hashlib.sha256).digest(), (seed, k)
            total += 1
    assert total == 200
    print("MEAS random200: 200/200 exact")


@cocotb.test()
async def test_hmac_wrongkey(dut):
    """Flipped key/msg bit avalanches the tag; job still completes clean."""
    from sim.provision import derive_keys as dk

    start_clock(dut)
    await quiesce(dut)
    await reset_dut(dut)
    key = dk(42).hmac_key
    msg = bytes(range(68))
    good, _ = await run_hmac(dut, key, msg)
    assert good == hmac_mod.new(key, msg, hashlib.sha256).digest()
    bad_key = bytearray(key)
    bad_key[0] ^= 1
    tag_k, rec_k = await run_hmac(dut, bytes(bad_key), msg)
    assert tag_k != good, "key bit-flip did not avalanche"
    assert len(rec_k["done_at"]) == 1 and rec_k["err_at"] is None
    bad_msg = bytearray(msg)
    bad_msg[17] ^= 0x80
    tag_m, _ = await run_hmac(dut, key, bytes(bad_msg))
    assert tag_m != good, "msg bit-flip did not avalanche"


@cocotb.test()
async def test_hmac_keylen_reject(dut):
    """key_len 161 (> KEY_MAX 160): error, no done, clean job after."""
    start_clock(dut)
    await quiesce(dut)
    await reset_dut(dut)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    dut.key_data_in.value = 0
    dut.key_len_in.value = 161
    await pulse_start(dut)
    for _ in range(200):
        await RisingEdge(dut.clk)
        await settle()
    assert rec["err_at"] is not None, "oversize key set no error"
    assert not rec["done_at"], "oversize key completed a tag"
    assert not read_sig(dut.busy_out, 1), "oversize key left busy set"
    rec["stop"] = True
    await mon
    key = derive_keys(7).hmac_key
    msg = b"after-error recovery"
    tag, rec2 = await run_hmac(dut, key, msg)  # start clears the error
    assert tag == hmac_mod.new(key, msg, hashlib.sha256).digest()
    assert rec2["err_at"] is None


@cocotb.test()
async def test_hmac_overflow_abort(dut):
    """Inject junk into the wrap mid-job: error, no done, clean job after."""
    start_clock(dut)
    await quiesce(dut)
    await reset_dut(dut)
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))
    dut.key_data_in.value = key_word(derive_keys(9).hmac_key)
    dut.key_len_in.value = 32
    dut.inject_en.value = 1  # wrap input = TB junk from job start
    dut.inject_valid.value = 1
    await pulse_start(dut)
    for k in range(400):
        dut.inject_data.value = k & 0xFF
        await RisingEdge(dut.clk)
        await settle()
    dut.inject_valid.value = 0
    assert rec["ovf_at"] is not None, "inject flood set no wrap overflow"
    assert rec["err_at"] is not None, "wrap overflow during HMAC set no hmac error"
    assert not rec["done_at"], "aborted job completed a tag"
    assert not read_sig(dut.busy_out, 1), "aborted job left busy set"
    rec["stop"] = True
    await mon
    dut.inject_en.value = 0  # hmac owns the wrap again; wrap re-inits cleanly
    key = derive_keys(9).hmac_key
    msg = b"post-abort recovery"
    tag, rec2 = await run_hmac(dut, key, msg)
    assert tag == hmac_mod.new(key, msg, hashlib.sha256).digest()
    assert rec2["err_at"] is None and rec2["ovf_at"] is None


@cocotb.test()
async def test_hmac_overlap(dut):
    """Item 2 (hmac level): chain N -> HMAC -> chain N+1 on one core, exact."""
    start_clock(dut)
    await quiesce(dut)
    await reset_dut(dut)
    rng = random.Random(61)
    msg_n = bytes(rng.randrange(256) for _ in range(2_000))
    msg_p = bytes(rng.randrange(256) for _ in range(1_000))
    desc = bytes(rng.randrange(256) for _ in range(36))
    key = derive_keys(61).hmac_key
    rec = new_rec()
    mon = cocotb.start_soon(start_monitor(dut, rec))

    async def chain_message(data: bytes) -> bytes:
        """Hash one chain message through the wrap via inject; return digest."""
        dut.inject_en.value = 1
        dut.inject_init.value = 1
        for _ in range(2):
            await RisingEdge(dut.clk)
        await settle()
        dut.inject_init.value = 0
        idx = 0
        n = len(data)
        while idx < n:
            if not read_sig(dut.tap_ready, 1):
                dut.inject_valid.value = 0
                dut.inject_last.value = 0
                await RisingEdge(dut.clk)
                await settle()
                continue
            dut.inject_data.value = data[idx]
            dut.inject_valid.value = 1
            dut.inject_last.value = 1 if idx == n - 1 else 0
            await RisingEdge(dut.clk)
            await settle()
            idx += 1
        dut.inject_valid.value = 0
        dut.inject_last.value = 0
        seen = len(rec["done_at"])  # chain completions share no counter; poll tap_wdv
        for _ in range(100_000):
            await RisingEdge(dut.clk)
            await settle()
            if read_sig(dut.tap_wdv, 1):
                break
        else:
            raise AssertionError("chain message never completed")
        assert seen == len(rec["done_at"])  # no hmac done during chain phase
        return read_sig(dut.tap_digest, 256).to_bytes(32, "big")

    digest_n = await chain_message(msg_n)  # N streams while hmac is idle
    assert digest_n == hashlib.sha256(msg_n).digest(), "chain N wrong"
    dut.inject_en.value = 0  # hand the wrap to hmac
    dut.key_data_in.value = key_word(key)
    dut.key_len_in.value = len(key)
    n_done_before = len(rec["done_at"])
    await pulse_start(dut)
    # N+1 arrives during the HMAC window: withhold (5d skid model).
    withhold_cycles = 0
    stream_task = cocotb.start_soon(stream_msg(dut, desc + digest_n))
    while read_sig(dut.busy_out, 1):
        await RisingEdge(dut.clk)
        await settle()
        withhold_cycles += 1
        assert withhold_cycles < 100_000, "hmac job never finished (abort/hang?)"
    await stream_task  # msg fully consumed during INNER_MSG
    # Scheduling slack: the done pulse and the monitor's sample share the
    # same timestep, so a one-shot assert can outrun the monitor. Poll
    # briefly (the pulse already fired — busy dropped on it).
    for _ in range(10):
        if len(rec["done_at"]) == n_done_before + 1:
            break
        await RisingEdge(dut.clk)
        await settle()
    assert len(rec["done_at"]) == n_done_before + 1, (
        f"job ended without done (err_at={rec['err_at']} ovf_at={rec['ovf_at']} "
        f"busy={read_sig(dut.busy_out, 1)} phase={read_sig(dut.phase_out, 4)})"
    )
    await stream_task  # msg fully consumed during INNER_MSG
    assert len(rec["done_at"]) == n_done_before + 1, (
        f"job ended without done (err_at={rec['err_at']} ovf_at={rec['ovf_at']} "
        f"busy={read_sig(dut.busy_out, 1)} phase={read_sig(dut.phase_out, 4)})"
    )
    tag = read_sig(dut.hmac_out, 256).to_bytes(32, "big")
    assert tag == hmac_mod.new(key, desc + digest_n, hashlib.sha256).digest()
    assert withhold_cycles > 100, withhold_cycles  # N+1 really spanned the window
    assert withhold_cycles == 473, withhold_cycles  # pinned: both sims identical
    assert rec["peak"] == 86, rec["peak"]  # pinned HMAC-stream fill (deterministic)
    digest_p = await chain_message(msg_p)  # wrap back to chain use, exact
    assert digest_p == hashlib.sha256(msg_p).digest(), "chain N+1 wrong"
    assert rec["ovf_at"] is None, "shared-core overlap set overflow"
    assert rec["err_at"] is None
    rec["stop"] = True
    await mon
    await quiesce(dut)
    print(f"MEAS hmac_overlap: hmac_window_cycles={withhold_cycles} peak_occ={rec['peak']}")
    with meas_open("hmac_overlap") as mf:
        mf.write(f"hmac_window_cycles={withhold_cycles} peak_occ={rec['peak']}\n")
