"""Week 5d2 cocotb: top.v integrated attestation core + skid_buf unit.

top.v wires uart_rx -> frame_rx -> zc_detect -> power_calc -> record_agg
-> (div_fsm p_avg) -> chain_ctrl -> hmac_core through ONE sha256_wrap,
with the tap (P_inst exact) feeding a 16-deep skid. The TB drives ONLY
the rx serial line (plus key provisioning); golden expectations compose
the EXISTING Python models (no new math): frames via edge.framing,
windows via the integer ZC golden (sensors.windows), power/descriptor
via tb.golden.record_fields, layout via edge.attestation, keys via
sim/provision. Window boundaries come from golden windows over the SENT
codes; DUT closes match them exactly (same detector contract as 4b).

PERFORMANCE (measured week5d2): a Python<->sim interaction costs
~0.2 ms, so per-edge background monitoring (~1 interaction/edge) turns
an 8.8M-clock record into 30+ min. This TB therefore sends in
per-window CHUNKS and reads LATCHED registers after fixed,
deterministic waits (power close is pinned 401; post-close ~1200) —
polled levels, never pulses. Exact event spans come from DUT-side
sticky timestamp taps (winend5/recdone/valid), also polled. The only
per-edge monitor left is pinst (tap pulses, small and proven ~11 s).
Fixed waits cannot hang (bounded loops); a slower-than-expected DUT
shows stale registers and fails loudly.
"""

import hashlib
import hmac as hmac_mod
import math
import os
import random
from pathlib import Path

import cocotb
import numpy as np
from cocotb.triggers import RisingEdge, Timer

from edge.attestation import descriptor_bytes
from edge.framing import encode_frame
from sim.provision import derive_keys
from tb.golden import record_fields
from tb.w4b_common import read_sig, reset_dut, settle, start_clock

REPO = Path(__file__).resolve().parents[1]

BIT_CYC = 8  # [clocks] UART bit time at CLK_PER_BIT=8 (TB clock 10 ns)
BIT_NS = 80.0  # [ns] nominal bit time (w4b send_bytes convention; exact float)
BASE_COUNTER = 100000  # [counts] L0 counter of the first sent sample
POST_CHUNK_WAIT = 600  # [edges] covers power close (pinned 401) + reg settle
POST_CLOSE_WAIT = 20000  # [edges] covers post-close pipeline with wide margin
# (2500 proved marginal and 6000 insufficient for later records: the true
# spans below, from sticky DUT stamps, size the principled bound)
# (record-2 reads showed the 2500 bound marginal; stamps below give exact spans)


def meas_open(name: str):
    """Durable measurement file (sim print()s vanish on pass; files don't)."""
    sim = os.environ.get("WEEK4B_TAG", "unknown")
    return open(REPO / "results" / f"meas_{name}_{sim}.txt", "w")


def meas_reset(name: str) -> None:
    """Drop a stale multi-record measurement file (append below is per-run)."""
    sim = os.environ.get("WEEK4B_TAG", "unknown")
    p = REPO / "results" / f"meas_{name}_{sim}.txt"
    if p.exists():
        p.unlink()


def meas_line(name: str, line: str) -> None:
    """Append one measurement line (multi-record tests keep every record)."""
    sim = os.environ.get("WEEK4B_TAG", "unknown")
    with open(REPO / "results" / f"meas_{name}_{sim}.txt", "a") as f:
        f.write(line + "\n")


def sample_bytes_of(v_codes, i_codes) -> bytes:
    """8 B/sample hash wire format: V<i2 || I<i2 || P=i32 (exact)."""
    out = bytearray()
    for v, i in zip(v_codes, i_codes):
        out += int(v).to_bytes(2, "little", signed=True)
        out += int(i).to_bytes(2, "little", signed=True)
        out += (int(v) * int(i)).to_bytes(4, "little", signed=True)
    return bytes(out)


async def send_frames(dut, frames: list[tuple[int, int, int]]) -> None:
    """Send (counter, v, i) frames back-to-back at wire rate (880/frame).

    Run-length line driver: one Timer per constant run (not per bit) —
    ~2x fewer Python<->sim interactions than per-bit timers at identical
    waveforms. Nominal rate only (baud tolerance stays unit-pinned).
    """
    levels: list[int] = []
    for c, v, i in frames:
        for byte in encode_frame(c, v, i):
            levels.append(0)  # start bit
            for b in range(8):
                levels.append((byte >> b) & 1)
            levels.append(1)  # stop bit
    # Compress to runs; 1 ns setup after edges is inherited from the
    # bit-grid phase (same convention as tb/w4b_uart_cocotb.send_byte).
    idx = 0
    n = len(levels)
    while idx < n:
        j = idx
        while j < n and levels[j] == levels[idx]:
            j += 1
        dut.rx.value = levels[idx]
        bit_ns = round((j - idx) * BIT_CYC * 10.0 * 1000) / 1000
        await Timer(bit_ns, unit="ns")
        idx = j


async def wait_edges(dut, n: int) -> None:
    """Advance n clock edges (bounded fixed waits never hang)."""
    for _ in range(n):
        await RisingEdge(dut.clk)
    await settle()


async def quiesce_top(dut) -> None:
    """Idle line + quiet inputs (before reset and between phases)."""
    dut.rx.value = 1
    dut.hmac_key_data_in.value = 0
    dut.hmac_key_valid_in.value = 0


async def provision(dut, key: bytes) -> None:
    """Write-once key load from sim/provision.py material; assert latched."""
    assert len(key) == 32
    dut.hmac_key_data_in.value = int.from_bytes(key, "little")
    dut.hmac_key_valid_in.value = 1
    for _ in range(2):
        await RisingEdge(dut.clk)
    await settle()
    dut.hmac_key_valid_in.value = 0
    await RisingEdge(dut.clk)
    await settle()
    assert read_sig(dut.hmac_provisioned_out, 1) == 1, "key not latched"


def read_window(dut) -> dict:
    """Latched window outputs (caller ensures a close settled)."""
    return {
        "vrms": read_sig(dut.window_vrms, 16),
        "irms": read_sig(dut.window_irms, 16),
        "pf": read_sig(dut.window_pf, 16, signed=True),
        "m": read_sig(dut.window_m, 12),
        "ok": int(dut.window_ok.value),
    }


def read_record(dut) -> dict:
    """Latched record outputs + sidecar + stamps (caller ensures valid)."""
    return {
        "counter": read_sig(dut.record_counter, 32),
        "wstart": read_sig(dut.record_window_start, 32),
        "zc": read_sig(dut.record_zc, 60),
        "flags": read_sig(dut.record_flags, 8),
        "overrun": read_sig(dut.record_overrun_cnt, 8),
        "p_avg": read_sig(dut.record_p_avg, 32, signed=True),
        "energy": read_sig(dut.record_energy, 64),
        "hash": read_sig(dut.record_hash, 256).to_bytes(32, "big"),
        "hmac": read_sig(dut.record_hmac, 256).to_bytes(32, "big"),
        "halted": read_sig(dut.halted_out, 1),
        "sc": (
            read_sig(dut.sc_crc_err_cnt, 16),
            read_sig(dut.sc_resync_cnt, 16),
            read_sig(dut.sc_ferr_cnt, 16),
            read_sig(dut.sc_skid_ovf_cnt, 16),
            read_sig(dut.sc_saturated, 1),
            read_sig(dut.sc_counter, 32),
        ),
        "skid_ovf": read_sig(dut.skid_overflow_out, 1),
        "skid_peak": read_sig(dut.tap_skid_peak, 5),
        "stamps": (
            read_sig(dut.tap_stamp_winend5, 32),
            read_sig(dut.tap_stamp_recdone, 32),
            read_sig(dut.tap_stamp_valid, 32),
        ),
    }


def golden_windows(v_codes):
    """Golden detector over sent codes: [(S_idx, E_idx, valid)] in order."""
    from sensors.windows import ZCParams, build_windows, find_rising_crossings_q15

    det, frac = find_rising_crossings_q15(np.array(v_codes, dtype=np.int64))
    wins = build_windows(det, frac, len(v_codes), ZCParams())
    return [(int(w.start_idx), int(w.end_idx), bool(w.valid)) for w in wins]


def check_record(
    dut_rec: dict,
    v_all,
    i_all,
    wins,
    energy_prev: int,
    counter: int,
    device: int,
    key: bytes,
    prev: bytes,
    base: int,
    counters=None,
    hash_span=None,
) -> bytes:
    """Full golden check of one DUT record over 5 golden windows.

    wins: [(S_idx, E_idx, golden_valid)] in window order (== arrival
    order: no overrun conditions in 5d2 tests). Tombstones (golden-valid
    but DUT-invalid+M0) count into overrun; anything else mismatched
    fails loud. hash_span=(hs, he): sample indices hashed into THIS
    record (differs from window spans at the edges: un-windowed preamble
    [0:S_0] hashes (drop-oblivious) but contributes no sums; the closing
    E belongs to the next record). Returns the verified window_hash.
    """
    assert len(wins) == 5, f"need 5 windows, got {len(wins)}"
    dut_zc = [(dut_rec["zc"] >> (12 * s)) & 0xFFF for s in range(5)]
    tombs = 0
    subs = []
    for k, (s, e, gvalid) in enumerate(wins):
        dut_ok = (dut_rec["flags"] >> k) & 1
        v = [int(x) for x in v_all[s:e]]
        i = [int(x) for x in i_all[s:e]]
        if gvalid and not dut_ok:
            assert dut_zc[k] == 0, f"slot {k}: tomb must read M 0"
            tombs += 1
            subs.append((v, i, False))
        elif dut_ok and not gvalid:
            raise AssertionError(f"slot {k}: DUT valid where golden invalid (detector mismatch)")
        else:
            if dut_ok:
                assert dut_zc[k] == e - s, f"slot {k}: M {dut_zc[k]} != golden {e - s}"
            else:
                assert dut_zc[k] == 0, f"slot {k}: invalid must read M 0"
            subs.append((v, i, dut_ok))
    rf = record_fields(subs, energy_prev, overrun_cnt=tombs)
    assert rf.window_flags == dut_rec["flags"], (hex(rf.window_flags), hex(dut_rec["flags"]))
    assert rf.p_avg_q30 == dut_rec["p_avg"], (rf.p_avg_q30, dut_rec["p_avg"])
    assert rf.energy_uwh == dut_rec["energy"], (rf.energy_uwh, dut_rec["energy"])
    assert rf.overrun_cnt == dut_rec["overrun"], (rf.overrun_cnt, dut_rec["overrun"])
    assert tuple(rf.zc_samples) == tuple(dut_zc), (rf.zc_samples, dut_zc)
    assert dut_rec["counter"] == counter, (dut_rec["counter"], counter)
    wstart = counters[wins[0][0]] if counters is not None else base + wins[0][0]
    assert dut_rec["wstart"] == wstart, (dut_rec["wstart"], wstart)
    desc = descriptor_bytes(
        counter,
        wstart,
        tuple(dut_zc),
        dut_rec["flags"],
        device,
        dut_rec["p_avg"],
        dut_rec["energy"],
        dut_rec["overrun"],
    )
    msg = bytes(prev)
    hs, he = hash_span if hash_span is not None else (wins[0][0], wins[-1][1])
    msg += sample_bytes_of(v_all[hs:he], i_all[hs:he])
    msg += desc
    if os.environ.get("DEBUG_HASH"):
        with open(
            REPO / "results" / f"meas_hashdbg_{os.environ.get('WEEK4B_TAG', '?')}.txt", "a"
        ) as f:
            g = hashlib.sha256(msg).digest()
            f.write(
                f"len={len(msg)} golden={g.hex()} dut={dut_rec['hash'].hex()} "
                f"desc={desc.hex()} flags={dut_rec['flags']:02x}\n"
            )
    assert hashlib.sha256(msg).digest() == dut_rec["hash"], "window hash != golden"
    assert hmac_mod.new(key, desc + dut_rec["hash"], hashlib.sha256).digest() == dut_rec["hmac"]
    assert dut_rec["halted"] == 0
    return dut_rec["hash"]


def check_window(got: dict, v_all, i_all, span) -> None:
    """One latched window output exact vs golden (zeros when invalid)."""
    from tb.golden import window_power

    s, e, gvalid = span
    if gvalid:
        assert got["ok"] == 1, "golden-valid window read invalid"
        g = window_power([int(x) for x in v_all[s:e]], [int(x) for x in i_all[s:e]])
        assert (got["vrms"], got["irms"], got["pf"], got["m"]) == (
            g.vrms_q15,
            g.irms_q15,
            g.pf_q15,
            g.m,
        ), (got, g)
    else:
        assert got["ok"] == 0
        assert (got["vrms"], got["irms"], got["pf"], got["m"]) == (0, 0, 0, 0)


def sine_frames(
    n_samples: int, f_hz: float, base: int, vpk: int = 20000, ipk: int = 10000, seed: int = 0
):
    """Deterministic sine (counter, v, i) frames [codes] at 10 kHz."""
    rng = random.Random(seed)
    out = []
    for k in range(n_samples):
        t = k / 10000.0
        v = int(vpk * math.sin(2 * math.pi * f_hz * t))
        i = int(ipk * math.sin(2 * math.pi * f_hz * t))
        v = max(-32768, min(32767, v + rng.randrange(-2, 3)))
        i = max(-32768, min(32767, i + rng.randrange(-1, 2)))
        out.append((base + k, v, i))
    return out


async def send_to_close(dut, frames, cur: int, e_win: int) -> int:
    """Send frames[cur..e_win] (inclusive): e_win's frame closes the window.

    Every sample streams EXACTLY once (no duplicates/skips): callers track
    cur (starts at a window S, or 0 for a preamble E serves the NEXT
    window's S). After the send, the close fired (E + power 401 < 600).
    Returns e_win + 1 (next unsent index). Chunks tile contiguously
    because golden windows do ([S,E) back-to-back, E_k == S_{k+1}).
    """
    await send_frames(dut, frames[cur : e_win + 1])
    await wait_edges(dut, POST_CHUNK_WAIT)
    return e_win + 1


@cocotb.test()
async def test_top_e2e_full(dut):
    """Mandatory full-shape record: nominal valid windows at wire rate (slow).

    50 Hz sine (200 samples/cycle, N=10 -> M ~= 2000, all valid), 5
    windows + E5 margin, back-to-back (880/frame). Golden: detector on
    sent codes -> windows -> record/hash/HMAC vs provisioned key.
    Derivation spans from sticky DUT stamps (exact, polled).
    """
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    device = 1
    frames = sine_frames(11_000, 50.0, BASE_COUNTER, seed=11)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 5, f"only {len(wins)} golden windows"
    wins = wins[:5]
    assert all(w[2] for w in wins), "full-shape windows must all be valid"
    # Preamble [0:S_0]: streamed (hashes) but windowless (closes nothing).
    # Windows stream through their closing E (inclusive) so each close
    # fires in-chunk; the E sample itself belongs to the NEXT window.
    cur = 0
    if wins[0][0]:
        await send_frames(dut, frames[0 : wins[0][0]])
        cur = wins[0][0]
    for s, e, ok in wins:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r = read_record(dut)
    assert r["counter"] == 0, r["counter"]
    tag = check_record(
        r,
        v_all,
        i_all,
        wins,
        0,
        0,
        device,
        km.hmac_key,
        bytes(32),
        BASE_COUNTER,
        hash_span=(0, wins[4][1]),
    )
    assert tag == r["hash"]
    assert r["sc"][:4] == (0, 0, 0, 0) and r["sc"][4] == 0, f"sidecar not clean: {r['sc']}"
    assert r["sc"][5] == 0, r["sc"]  # sidecar carries its record counter
    assert r["skid_ovf"] == 0
    assert r["skid_peak"] == 8, r["skid_peak"]  # pinned wire-rate peak, both sims
    we5, rd, va = r["stamps"]
    assert va - we5 == 1120, va - we5  # pinned post-close shape, both sims
    assert va - rd == 718, va - rd
    assert (va - we5) - (va - rd) == 402  # 401 power latency + 1 stamp edge
    assert POST_CLOSE_WAIT >= 10 * (va - rd)  # >=10x margin
    with meas_open("top_e2e_full") as mf:
        mf.write(f"winend5_to_valid={va - we5}\n")
        mf.write(f"rec_done_to_valid={va - rd}\n")
        mf.write(f"skid_peak={r['skid_peak']}\n")


async def run_e2e_small(dut, device: int, seed: int, tag: str):
    """Fast-invalid record helper (500 Hz crossings, M ~= 200, invalid)."""
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    frames = sine_frames(1_400, 500.0, BASE_COUNTER, seed=seed)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 5, f"only {len(wins)} golden windows"
    wins = wins[:5]
    assert not any(w[2] for w in wins), "small-shape windows must all be invalid"
    cur = 0
    if wins[0][0]:
        await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
        cur = wins[0][0]
    for s, e, ok in wins:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r = read_record(dut)
    check_record(
        r,
        v_all,
        i_all,
        wins,
        0,
        0,
        device,
        km.hmac_key,
        bytes(32),
        BASE_COUNTER,
        hash_span=(0, wins[4][1]),
    )
    assert r["sc"][5] == 0, r["sc"]  # sidecar carries its record counter
    we5, rd, va = r["stamps"]
    assert (va - we5, va - rd) == (1011, 609), (va - we5, va - rd)  # genesis-invalid
    assert (va - we5) - (va - rd) == 402, (va - we5, va - rd)  # 401 power + 1
    assert POST_CLOSE_WAIT >= 10 * (va - rd), va - rd  # >=10x margin
    with meas_open(tag) as mf:
        mf.write(f"winend5_to_valid={va - we5}\n")
        mf.write(f"rec_done_to_valid={va - rd}\n")
        mf.write(f"skid_peak={r['skid_peak']}\n")
    return r


@cocotb.test()
async def test_top_e2e_small(dut):
    """Fast-invalid record: fields/hash/HMAC exact vs golden."""
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    await run_e2e_small(dut, 1, 12, "top_e2e_small")


@cocotb.test()
async def test_top_e2e_device7(dut):
    """Same as small, DEVICE_ID=7 via parameters (descriptor/HMAC flow)."""
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    device = int(os.environ.get("EXPECTED_DEVICE", "1"))
    await run_e2e_small(dut, device, 12, "top_e2e_device7")


async def start_tap_monitor(dut, rec: dict):
    """Per-edge tap collector (pinst only: small, bounded)."""
    while not rec["stop"]:
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.tap_sample_valid, 1):
            rec["tap_bytes"].append(read_sig(dut.tap_sample_byte, 8))


@cocotb.test()
async def test_top_pinst_exact(dut):
    """Tap P_inst bytes exact on corners (-32768^2) + seeded randoms.

    Two frames per reset chunk: with no windows open the chain never
    drains, so the 16-deep skid would stall a long stream (by design —
    the bound is proven in test_top_skid_bound). Chunking keeps every
    byte observable; order and exactness checked per chunk.
    """
    start_clock(dut)
    pairs = [
        (0, 0),
        (1, 1),
        (-1, -1),
        (32767, 32767),
        (-32768, -32768),
        (-32768, 32767),
        (32767, -32768),
        (12345, -6789),
    ]
    rng = random.Random(77)
    for _ in range(40):
        pairs.append((rng.randrange(-32768, 32768), rng.randrange(-32768, 32768)))
    assert len(pairs) % 2 == 0
    rec = {"stop": False, "tap_bytes": []}
    mon = cocotb.start_soon(start_tap_monitor(dut, rec))
    for c in range(0, len(pairs), 2):
        await quiesce_top(dut)
        await reset_dut(dut)
        n0 = len(rec["tap_bytes"])
        frames = [(BASE_COUNTER + c + k, v, i) for k, (v, i) in enumerate(pairs[c : c + 2])]
        await send_frames(dut, frames)
        for _ in range(5000):
            await RisingEdge(dut.clk)
            await settle()
            if len(rec["tap_bytes"]) >= n0 + 16:
                break
        else:
            raise AssertionError(f"chunk {c}: tap stalled")
        exp = bytearray()
        for v, i in pairs[c : c + 2]:
            exp += int(v).to_bytes(2, "little", signed=True)
            exp += int(i).to_bytes(2, "little", signed=True)
            exp += (int(v) * int(i)).to_bytes(4, "little", signed=True)
        got = bytes(rec["tap_bytes"][n0 : n0 + 16])
        assert got == bytes(exp), f"chunk {c}: tap bytes != exact V||I||P"
    rec["stop"] = True
    await mon
    await quiesce_top(dut)


@cocotb.test()
async def test_top_boundary_E(dut):
    """Record-closing E hashes in the NEXT record (mutant: in current).

    Two fast-invalid records; spans split AT E from golden indices.
    Exclusion is proven by exact digests on BOTH sides (the spans differ
    by exactly E's 8 bytes, so including E anywhere wrong breaks a hash).
    """
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    device = 1
    frames = sine_frames(2_600, 500.0, BASE_COUNTER, seed=13)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 10, f"only {len(wins)} golden windows"
    await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
    cur = wins[0][0]
    for s, e, ok in wins[:5]:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r0 = read_record(dut)
    w0 = wins[:5]
    e = w0[4][1]  # boundary E: first sample owned by record 1 (== S_5)
    tag0 = check_record(
        r0,
        v_all,
        i_all,
        w0,
        0,
        0,
        device,
        km.hmac_key,
        bytes(32),
        BASE_COUNTER,
        hash_span=(0, e),
    )
    for s, e_, ok in wins[5:10]:
        cur = await send_to_close(dut, frames, cur, e_)
        check_window(read_window(dut), v_all, i_all, (s, e_, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r1 = read_record(dut)
    assert r1["counter"] == 1, r1["counter"]
    w1 = wins[5:10]
    # Record 1 spans [e, E9): E included first (owned by window 5).
    # E excluded from record 0 above IS the exclusion proof: both exact.
    tag1 = check_record(
        r1,
        v_all,
        i_all,
        w1,
        r0["energy"],
        1,
        device,
        km.hmac_key,
        tag0,
        BASE_COUNTER,
        hash_span=(e, w1[4][1]),
    )
    assert tag1 == r1["hash"]


@cocotb.test()
async def test_top_sidecar(dut):
    """Link-health counts latched/cleared; hash over accepted-only.

    Dirty record (CRC-bad frames, no SOF created) then a clean
    continuation WITHOUT reset: sidecar must show the dirty counts on
    record 0 and zeros on record 1 (close-latch then clear). Golden
    runs over the accepted subsequence (drops vanish); counters map
    accepted indices back to L0 counters. Per-window checks are
    intentionally record-level here (all other tests pin windows).
    """
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    device = 1
    frames_a = sine_frames(1_400, 500.0, BASE_COUNTER, seed=21)
    # Dirty frames confined to record 0's span (< 1000 sent): phase-A tail
    # (sent 1000+) lands in record 1's span, so dirtying it would count
    # into r1's sidecar (found: r1 read (8,0,0) from tail bad at 1025+).
    bad_idx = set(range(25, 1000, 50))
    tx = []
    for k, (c, v, i) in enumerate(frames_a):
        f = bytearray(encode_frame(c, v, i))
        if k in bad_idx:
            f[3] ^= 0x01  # payload bit (counter LSB byte, never SOF)
        tx.append(bytes(f))
    from tb.w4b_uart_cocotb import send_bytes as raw_send

    await raw_send(dut, b"".join(tx), bit_ns=BIT_NS)
    await wait_edges(dut, POST_CLOSE_WAIT)
    r0 = read_record(dut)
    assert r0["counter"] == 0, r0["counter"]
    assert r0["sc"][5] == 0, r0["sc"]  # sidecar carries its record counter
    # Clean continuation (counters continue; accepted == sent here).
    # NOTE: r0 is read BEFORE phase-B traffic (record 1 would overwrite).
    frames_b = sine_frames(1_400, 500.0, BASE_COUNTER + len(frames_a), seed=22)
    v_ok = [v for k, (_, v, _) in enumerate(frames_a) if k not in bad_idx]
    i_ok = [i for k, (_, _, i) in enumerate(frames_a) if k not in bad_idx]
    c_ok = [c for k, (c, _, _) in enumerate(frames_a) if k not in bad_idx]
    v_c = v_ok + [v for _, v, _ in frames_b]
    i_c = i_ok + [i for _, _, i in frames_b]
    c_c = c_ok + [c for c, _, _ in frames_b]
    wins = golden_windows(v_c)
    assert len(wins) >= 10, f"only {len(wins)} golden windows (concatenated)"
    await raw_send(dut, b"".join(encode_frame(c, v, i) for c, v, i in frames_b), bit_ns=BIT_NS)
    await wait_edges(dut, POST_CLOSE_WAIT)
    r1 = read_record(dut)
    assert r1["counter"] == 1, r1["counter"]
    assert r1["sc"][5] == 1, r1["sc"]  # sidecar carries its record counter
    assert (r1["sc"][0], r1["sc"][1], r1["sc"][2]) == (0, 0, 0), r1["sc"]  # cleared
    # Bad frames within record 0's span only: accepted E_4 maps to sent
    # domain by fixed point (sparse drops converge in two rounds).
    E4 = wins[4][1]
    J = E4
    for _ in range(3):
        J = E4 + sum(1 for b in bad_idx if b < J)
    n_bad_r0 = sum(1 for b in bad_idx if b < J)
    assert (r0["sc"][0], r0["sc"][1], r0["sc"][2]) == (n_bad_r0, 0, 0), (r0["sc"], n_bad_r0)
    assert r0["sc"][4] == 0, r0["sc"]
    tag0 = check_record(
        r0,
        v_c,
        i_c,
        wins[:5],
        0,
        0,
        device,
        km.hmac_key,
        bytes(32),
        BASE_COUNTER,
        counters=c_c,
        hash_span=(0, wins[4][1]),
    )
    tag1 = check_record(
        r1,
        v_c,
        i_c,
        wins[5:10],
        r0["energy"],
        1,
        device,
        km.hmac_key,
        tag0,
        BASE_COUNTER,
        counters=c_c,
        hash_span=(wins[4][1], wins[9][1]),
    )
    assert tag1 == r1["hash"]


@cocotb.test()
async def test_top_skid_bound(dut):
    """Wire-rate skid peak < 16 over a record incl. HMAC windows; no flag."""
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    frames = sine_frames(1_400, 500.0, BASE_COUNTER, seed=31)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)[:5]
    await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
    cur = wins[0][0]
    for s, e, ok in wins:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r = read_record(dut)
    check_record(
        r,
        v_all,
        i_all,
        wins,
        0,
        0,
        1,
        km.hmac_key,
        bytes(32),
        BASE_COUNTER,
        hash_span=(0, wins[4][1]),
    )
    assert read_sig(dut.tap_skid_peak, 5) < 16
    assert read_sig(dut.tap_skid_peak, 5) == 8, "wire-rate peak moved"
    assert r["skid_ovf"] == 0
    assert r["sc"][3] == 0
    with meas_open("top_skid_bound") as mf:
        mf.write(f"skid_peak={read_sig(dut.tap_skid_peak, 5)}\n")


@cocotb.test()
async def test_top_continuous_clean(dut):
    """Continuous 2-record flow stays exact: accumulation never overflows.

    Harshest reachable flow (no inter-record idle): the next job cannot
    start until the previous HMAC/T_OUT, so arrivals accumulate on top of
    E's held 8 B — occupancy peaks ~15 but never reaches 17 (no drop, no
    flag; measured: r0/r1 both bit-exact, sc drop count 0). Chunked flows
    never accumulate at all (peak pinned == 8). Together with the
    skid_buf-level drop+flag pin (test_skid_overflow17), this bounds the
    production trigger to unreachable-by-construction; VPI fault
    injection was attempted and does not stick on this Verilator setup
    (landing readback 0), so no e2e flag-flip exists — the consumption
    path (bit7 + invalidate, top.v:776-784) is covered by inspection plus
    the 5d3 verifier rule below.
    """
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    device = 1
    frames = sine_frames(2_600, 500.0, BASE_COUNTER, seed=13)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 10, f"only {len(wins)} golden windows"
    await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
    cur = wins[0][0]
    for s, e, ok in wins[:5]:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    # NO inter-record wait: keep the pipe full so the job-start latency
    # accumulates against E_4's held bytes. Catch r0 after its T_OUT
    # (T_OUT-1 lands ~5 windows later — no rush, poll-then-read).
    for _ in range(25):
        await wait_edges(dut, 200)
        if read_sig(dut.u_cnt.count_out, 32) == 1:
            break
    assert read_sig(dut.u_cnt.count_out, 32) == 1, "record 0 never published"
    r0 = read_record(dut)
    e4 = wins[4][1]
    tag0 = check_record(
        r0,
        v_all,
        i_all,
        wins[:5],
        0,
        0,
        device,
        km.hmac_key,
        bytes(32),
        BASE_COUNTER,
        hash_span=(0, e4),
    )
    assert tag0 == r0["hash"]
    for s, e, ok in wins[5:10]:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r1 = read_record(dut)
    assert r1["counter"] == 1, r1["counter"]
    assert r1["flags"] & 0x80 == 0, hex(r1["flags"])  # no overflow, still clean
    assert r1["skid_ovf"] == 0
    assert r1["sc"][3] == 0, r1["sc"]  # zero dropped bytes: trigger unreachable
    assert r1["sc"][5] == 1, r1["sc"]
    tag1 = check_record(
        r1,
        v_all,
        i_all,
        wins[5:10],
        r0["energy"],
        1,
        device,
        km.hmac_key,
        tag0,
        BASE_COUNTER,
        hash_span=(e4, wins[9][1]),
    )
    assert tag1 == r1["hash"]
    assert r1["skid_peak"] == 8, r1["skid_peak"]  # harshest flow still peaks 8
    meas_reset("top_continuous_clean")
    meas_line("top_continuous_clean", f"peak={r1['skid_peak']}")


@cocotb.test()
async def test_top_skid_overflow_e2e(dut):
    """Skid overflow end to end at SKID_DEPTH=4: drop -> bit 7 + invalidate.

    Depth 4 (test-only override via parameters=; production/CI/synth run
    16, repo-pinned) makes burst-vs-drain races drop bytes: the 5th
    concurrent byte drops AND flags (exactly one per record here, both
    sims). Full-shape valid windows (50 Hz, M ~= 2000) make the
    invalidation PROOF strong: detection stays intact (zc slots ==
    golden M) and power still measures (nonzero), while the published
    record carries bit 7, cleared valid bits, p_avg 0 and excluded
    energy. Record 1 proves "unchanged" (energy == record 0's); the
    sticky output is observed raised pre-consumption, the sidecar counts
    the dropped byte, and the tag still binds the published fields (no
    golden span — drop holes sit inside the hashed spans).
    """
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    device = 1
    frames = sine_frames(21_000, 50.0, BASE_COUNTER, seed=11)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 10, f"only {len(wins)} golden windows"
    assert all(w[2] for w in wins[:10]), "overflow-flow windows must all be valid"
    meas_reset("top_skid_overflow_e2e")
    await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
    cur = wins[0][0]
    for s, e, ok in wins[:4]:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    # Last window: send WITHOUT the post-chunk wait, then poll the sticky
    # output inside the close window (raised by drops during the record,
    # consumed at assembly ~467 edges after E_4's frame).
    s, e, ok = wins[4]
    await send_frames(dut, frames[cur : e + 1])
    cur = e + 1
    seen = 0
    for _ in range(8):
        await wait_edges(dut, 50)
        seen |= read_sig(dut.skid_overflow_out, 1)
    assert seen == 1, "skid overflow never raised at depth 4"
    await wait_edges(dut, 200)  # total 600 post-frame: close fired (+401)
    check_window(read_window(dut), v_all, i_all, (s, e, ok))
    for _ in range(25):  # catch r0 after its T_OUT (T_OUT-1 lands much later)
        await wait_edges(dut, 200)
        if read_sig(dut.u_cnt.count_out, 32) == 1:
            break
    assert read_sig(dut.u_cnt.count_out, 32) == 1, "record 0 never published"
    await wait_edges(dut, POST_CLOSE_WAIT)
    r0 = read_record(dut)
    assert r0["counter"] == 0, r0["counter"]
    assert r0["flags"] & 0x80 == 0x80, hex(r0["flags"])  # HASH_OVERFLOW set
    assert r0["flags"] & 0x1F == 0, hex(r0["flags"])  # valid bits cleared
    assert r0["p_avg"] == 0, r0["p_avg"]  # invalidated
    assert r0["energy"] == 0, r0["energy"]  # genesis: previous cumulative
    assert r0["skid_ovf"] == 0, "flag not consumed at assembly"
    assert r0["sc"][3] == 1, r0["sc"]  # exactly one dropped byte, both sims
    assert r0["sc"][5] == 0, r0["sc"]
    assert r0["halted"] == 0
    # Well-formed: the tag binds the published (flagged) fields (no
    # golden span — drop holes sit inside the hashed spans).
    dut_zc0 = [(r0["zc"] >> (12 * s)) & 0xFFF for s in range(5)]
    for k, (s, e, ok) in enumerate(wins[:5]):
        assert dut_zc0[k] == e - s, (k, dut_zc0[k], e - s)  # detection intact
    desc0 = descriptor_bytes(
        0,
        r0["wstart"],
        tuple(dut_zc0),
        r0["flags"],
        device,
        0,
        r0["energy"],
        r0["overrun"],
    )
    assert hmac_mod.new(km.hmac_key, desc0 + r0["hash"], hashlib.sha256).digest() == r0["hmac"], (
        "flagged record HMAC != recomputed"
    )
    meas_line(
        "top_skid_overflow_e2e",
        f"rec0 flags={r0['flags']:#04x} drops={r0['sc'][3]} peak={r0['skid_peak']}",
    )
    for s, e, ok in wins[5:10]:
        cur = await send_to_close(dut, frames, cur, e)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
    await wait_edges(dut, POST_CLOSE_WAIT)
    r1 = read_record(dut)
    assert r1["counter"] == 1, r1["counter"]
    assert r1["flags"] & 0x80 == 0x80, hex(r1["flags"])  # HASH_OVERFLOW set
    assert r1["flags"] & 0x1F == 0, hex(r1["flags"])  # valid bits cleared
    assert r1["p_avg"] == 0, r1["p_avg"]  # invalidated (computed nonzero: zc intact)
    assert r1["energy"] == r0["energy"], (r1["energy"], r0["energy"])  # unchanged
    assert r1["skid_ovf"] == 0, "flag not consumed at assembly"
    assert r1["sc"][3] == 1, r1["sc"]  # exactly one dropped byte, both sims
    assert r1["sc"][5] == 1, r1["sc"]
    assert r1["halted"] == 0
    dut_zc1 = [(r1["zc"] >> (12 * s)) & 0xFFF for s in range(5)]
    for k, (s, e, ok) in enumerate(wins[5:10]):
        assert dut_zc1[k] == e - s, (k, dut_zc1[k], e - s)  # detection intact
    desc1 = descriptor_bytes(
        1,
        r1["wstart"],
        tuple(dut_zc1),
        r1["flags"],
        device,
        0,
        r1["energy"],
        r1["overrun"],
    )
    assert hmac_mod.new(km.hmac_key, desc1 + r1["hash"], hashlib.sha256).digest() == r1["hmac"], (
        "flagged record HMAC != recomputed"
    )
    meas_line(
        "top_skid_overflow_e2e",
        f"rec1 flags={r1['flags']:#04x} drops={r1['sc'][3]} peak={r1['skid_peak']}",
    )


@cocotb.test()
async def test_top_key_provision(dut):
    """Double-provision ignored (HMAC under key1); unprovisioned close errors."""
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    k1 = derive_keys(42).hmac_key
    k2 = derive_keys(43).hmac_key
    await provision(dut, k1)
    await provision(dut, k2)  # ignored: write-once (assert HMAC under k1 below)
    frames = sine_frames(1_400, 500.0, BASE_COUNTER, seed=41)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)[:5]

    async def send_record_stream():
        await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
        cur = wins[0][0]
        for s, e, ok in wins:
            cur = await send_to_close(dut, frames, cur, e)
            check_window(read_window(dut), v_all, i_all, (s, e, ok))
        await wait_edges(dut, POST_CLOSE_WAIT)

    await send_record_stream()
    r = read_record(dut)
    tag = check_record(
        r, v_all, i_all, wins, 0, 0, 1, k1, bytes(32), BASE_COUNTER, hash_span=(0, wins[4][1])
    )
    assert tag == r["hash"]  # (oracle used k1: second provision ignored)
    # Reset without provisioning: close must error with NO record.
    await reset_dut(dut)
    await quiesce_top(dut)
    c0 = read_sig(dut.record_counter, 32)
    await send_record_stream()
    assert read_sig(dut.error_out, 1) == 1, "unprovisioned close set no error"
    assert read_sig(dut.record_counter, 32) == c0, "unprovisioned close emitted a record"
    # Recovery protocol (B2): reset + provision + resend genesis-fresh.
    # (Post-error continuation without reset would chain onto a held
    # digest with stale skid bytes — reset is the documented recovery.)
    await reset_dut(dut)
    await quiesce_top(dut)
    await provision(dut, k1)
    await send_record_stream()
    assert read_sig(dut.error_out, 1) == 0, "error not cleared by recovery"
    r3 = read_record(dut)
    check_record(
        r3, v_all, i_all, wins, 0, 0, 1, k1, bytes(32), BASE_COUNTER, hash_span=(0, wins[4][1])
    )


@cocotb.test()
async def test_top_counter_path(dut):
    """Counters 0..3 across 4 back-to-back records; wstart continuity; halted 0.

    Four contiguous records (no reset) exercise chain->HMAC->chain three
    times through the mux boundaries; every record is hash/HMAC-exact vs
    the Python oracle with prev chaining, so a boundary mis-route fails
    loud instead of wedging silently.
    """
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(42)
    await provision(dut, km.hmac_key)
    device = 1
    frames = sine_frames(5_200, 500.0, BASE_COUNTER, seed=51)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 20, f"only {len(wins)} golden windows"
    energy = 0
    prev = bytes(32)
    rec_prev: dict | None = None
    # Pinned post-close shapes (winend5_to_valid, rec_done_to_valid),
    # exact both sims: rec0 genesis; rec1-3 chained (+27 prev stream);
    # rec1/rec3 vs rec2 (+8 arrival-phase jitter, deterministic).
    EXPECTED = {0: (1019, 617), 1: (1054, 652), 2: (1046, 644), 3: (1054, 652)}
    meas_reset("top_counter_path")
    await send_frames(dut, frames[0 : wins[0][0]])  # preamble (hashes, windowless)
    cur = wins[0][0]
    for k in range(4):
        wk = wins[5 * k : 5 * k + 5]
        for s, e, ok in wk:
            cur = await send_to_close(dut, frames, cur, e)
            check_window(read_window(dut), v_all, i_all, (s, e, ok))
        await wait_edges(dut, POST_CLOSE_WAIT)
        r = read_record(dut)
        assert r["counter"] == k, (
            k,
            r["counter"],
            read_sig(dut.u_cnt.count_out, 32),
            int(dut.win_end_cnt.value),
            int(dut.tap_phase.value),
            int(dut.u_chain.phase_out.value),
            int(dut.u_chain.busy_out.value),
            int(dut.u_hmac.busy_out.value),
        )
        assert r["halted"] == 0
        assert r["sc"][5] == k, (k, r["sc"])  # sidecar carries its record counter
        e_prev = wins[5 * k - 1][1] if k else 0
        e_last = wk[4][1]
        if k:
            assert rec_prev is not None
            # Gap rule (5b) end to end: next start == prev start +
            # attested span + unattested gap, all from golden indices.
            prev_wk = wins[5 * (k - 1) : 5 * k]
            prev_att = sum((e - s) if ok else 0 for s, e, ok in prev_wk)
            exp_gap = (wk[0][0] - prev_wk[0][0]) - prev_att
            assert r["wstart"] - rec_prev["wstart"] - prev_att == exp_gap, (k, r["wstart"])
        prev = check_record(
            r,
            v_all,
            i_all,
            wk,
            energy,
            k,
            device,
            km.hmac_key,
            prev,
            BASE_COUNTER,
            hash_span=(e_prev, e_last),
        )
        energy = r["energy"]
        rec_prev = r
        we5, rd, va = r["stamps"]
        assert (va - we5, va - rd) == EXPECTED[k], (k, va - we5, va - rd)
        assert (va - we5) - (va - rd) == 402, (k, va - we5, va - rd)  # 401 power + 1
        assert POST_CLOSE_WAIT >= 10 * (va - rd), (k, va - rd)  # >=10x margin
        meas_line(
            "top_counter_path", f"rec{k} winend5_to_valid={va - we5} rec_done_to_valid={va - rd}"
        )


async def skid_step(dut, wr_valid: int, wr_data: int, rd_ready: int) -> tuple[int, int, int]:
    """One skid edge: drive wr/rd handshake; return (rd_valid, rd_data, ovf)."""
    dut.wr_valid.value = wr_valid
    dut.wr_data.value = wr_data & 0xFF
    dut.rd_ready.value = rd_ready
    await RisingEdge(dut.clk)
    await settle()
    out = (int(dut.rd_valid.value), int(dut.rd_data.value), int(dut.overflow_out.value))
    dut.wr_valid.value = 0
    dut.rd_ready.value = 0
    return out


@cocotb.test()
async def test_skid_order(dut):
    """16 in, 16 out in order; flags clear; occupancy exact."""
    start_clock(dut)
    dut.wr_valid.value = 0
    dut.wr_data.value = 0
    dut.rd_ready.value = 0
    dut.flag_consume.value = 0
    dut.cnt_clear.value = 0
    await reset_dut(dut)
    assert read_sig(dut.occ_out, 5) == 0
    assert read_sig(dut.overflow_out, 1) == 0
    for k in range(16):  # fill with pop held
        await skid_step(dut, 1, 0xA0 + k, 0)
    assert read_sig(dut.occ_out, 5) == 16
    assert read_sig(dut.rd_valid, 1) == 1
    assert read_sig(dut.rd_data, 8) == 0xA0, "FWFT head != first byte"
    for k in range(16):  # pop k reveals head k+1 (FWFT); last pop empties
        _, data, _ = await skid_step(dut, 0, 0, 1)
        if k < 15:
            assert data == 0xA0 + k + 1, (k, hex(data))
    assert read_sig(dut.occ_out, 5) == 0
    assert read_sig(dut.rd_valid, 1) == 0
    assert read_sig(dut.overflow_out, 1) == 0


@cocotb.test()
async def test_skid_stream(dut):
    """Sustained concurrent push+pop (1 B/cyc): order exact, occ stable.

    Regression for the top.v valid/data coherence class (week5d2): the
    sequential order/overflow tests never push and pop on the SAME edge,
    but production streams that way. Pushes 64 bytes while popping from
    the start; every popped byte must equal its pushed value in order,
    occupancy must return to 0, no overflow may set.
    """
    start_clock(dut)
    dut.wr_valid.value = 0
    dut.wr_data.value = 0
    dut.rd_ready.value = 0
    dut.flag_consume.value = 0
    dut.cnt_clear.value = 0
    await reset_dut(dut)
    await skid_step(dut, 1, 0x11, 0)  # prime one byte (pop path live below)
    exp = [0x11]
    for k in range(64):
        # Pre-edge head == byte this edge will pop (registered-read rule).
        assert read_sig(dut.rd_data, 8) == exp[0], (k, hex(read_sig(dut.rd_data, 8)))
        assert read_sig(dut.rd_valid, 1) == 1
        dut.wr_valid.value = 1
        dut.wr_data.value = (0x30 + k) & 0xFF
        dut.rd_ready.value = 1
        await RisingEdge(dut.clk)
        await settle()
        assert read_sig(dut.overflow_out, 1) == 0, f"overflow at k={k}"
        dut.wr_valid.value = 0
        dut.rd_ready.value = 0
        exp.pop(0)
        exp.append((0x30 + k) & 0xFF)
    assert read_sig(dut.occ_out, 5) == 1, read_sig(dut.occ_out, 5)  # primed+64-64
    assert read_sig(dut.rd_data, 8) == 0x6F, hex(read_sig(dut.rd_data, 8))  # last pushed
    _, _, _ = await skid_step(dut, 0, 0, 1)
    assert read_sig(dut.occ_out, 5) == 0
    assert read_sig(dut.rd_valid, 1) == 0
    assert read_sig(dut.overflow_out, 1) == 0


@cocotb.test()
async def test_skid_overflow17(dut):
    """17th byte sets sticky overflow; 16 drain in order; recovery exact."""
    start_clock(dut)
    dut.wr_valid.value = 0
    dut.wr_data.value = 0
    dut.rd_ready.value = 0
    dut.flag_consume.value = 0
    dut.cnt_clear.value = 0
    await reset_dut(dut)
    for k in range(16):
        await skid_step(dut, 1, k, 0)
    _, _, ovf = await skid_step(dut, 1, 0xEE, 0)  # 17th: dropped AND flagged
    assert ovf == 1, "17th byte set no overflow flag"
    assert read_sig(dut.drop_cnt_out, 16) == 1, "drop count != 1"
    assert read_sig(dut.occ_out, 5) == 16, "overflow grew the queue"
    assert read_sig(dut.rd_data, 8) == 0, "overflow disturbed the head"
    for k in range(16):  # first 16 intact (dropped byte is gone, flagged)
        _, data, _ = await skid_step(dut, 0, 0, 1)
        if k < 15:
            assert data == k + 1, (k, data)
    assert read_sig(dut.occ_out, 5) == 0
    assert read_sig(dut.overflow_out, 1) == 1, "flag not sticky"
    for k in range(4):  # recovery: exact after the event
        await skid_step(dut, 1, 0x40 + k, 0)
    assert read_sig(dut.rd_data, 8) == 0x40
    for k in range(4):
        _, data, _ = await skid_step(dut, 0, 0, 1)
        if k < 3:
            assert data == 0x40 + k + 1
    await reset_dut(dut)
    assert read_sig(dut.overflow_out, 1) == 0, "reset did not clear flag"
    assert read_sig(dut.occ_out, 5) == 0


@cocotb.test()
async def test_skid_depth4(dut):
    """DEPTH=4 threshold: 4 in order, 5th dropped AND flagged; recovery exact.

    Proves the SKID_DEPTH parameterization (top.v passes SKID_DEPTH;
    production/CI/synth run the default 16): the drop+flag threshold
    follows the parameter, order is preserved across the wrap, and reset
    clears. Runs on toplevel=skid_buf with parameters={DEPTH: 4}.
    """
    start_clock(dut)
    dut.wr_valid.value = 0
    dut.wr_data.value = 0
    dut.rd_ready.value = 0
    dut.flag_consume.value = 0
    dut.cnt_clear.value = 0
    await reset_dut(dut)
    assert read_sig(dut.occ_out, 5) == 0
    for k in range(4):  # fill with pop held
        await skid_step(dut, 1, 0xB0 + k, 0)
    assert read_sig(dut.occ_out, 5) == 4
    assert read_sig(dut.overflow_out, 1) == 0
    assert read_sig(dut.rd_data, 8) == 0xB0, "FWFT head != first byte"
    _, _, ovf = await skid_step(dut, 1, 0xEE, 0)  # 5th: dropped AND flagged
    assert ovf == 1, "5th byte set no overflow flag at depth 4"
    assert read_sig(dut.drop_cnt_out, 16) == 1, "drop count != 1"
    assert read_sig(dut.occ_out, 5) == 4, "overflow grew the queue"
    assert read_sig(dut.rd_data, 8) == 0xB0, "overflow disturbed the head"
    for k in range(4):  # first 4 intact, in order (wrap at DEPTH-1)
        _, data, _ = await skid_step(dut, 0, 0, 1)
        if k < 3:
            assert data == 0xB0 + k + 1, (k, hex(data))
    assert read_sig(dut.occ_out, 5) == 0
    assert read_sig(dut.overflow_out, 1) == 1, "flag not sticky"
    # Consume handshake: flag clears, count kept (drop-wins verified below).
    dut.flag_consume.value = 1
    await skid_step(dut, 0, 0, 1)
    dut.flag_consume.value = 0
    assert read_sig(dut.overflow_out, 1) == 0, "consume did not clear flag"
    assert read_sig(dut.drop_cnt_out, 16) == 1, "consume disturbed the count"
    # Clear discipline: count resets, new drops recount from 0.
    dut.cnt_clear.value = 1
    await skid_step(dut, 0, 0, 0)
    dut.cnt_clear.value = 0
    assert read_sig(dut.drop_cnt_out, 16) == 0, "clear did not reset count"
    for k in range(6):  # recovery across the wrap: 4 accepted, 2 dropped
        await skid_step(dut, 1, 0xC0 + k, 0)
    assert read_sig(dut.drop_cnt_out, 16) == 2, "drop count != 0+2 post-clear"
    assert read_sig(dut.occ_out, 5) == 4
    assert read_sig(dut.rd_data, 8) == 0xC0
    for k in range(4):  # drain the 4 accepted, head advances (FWFT)
        _, data, _ = await skid_step(dut, 0, 0, 1)
    assert read_sig(dut.occ_out, 5) == 0
    # Drop-wins: consume edge simultaneous with a drop keeps the flag.
    for k in range(4):
        await skid_step(dut, 1, 0xD0 + k, 0)
    dut.flag_consume.value = 1
    _, _, ovf = await skid_step(dut, 1, 0xEE, 0)
    dut.flag_consume.value = 0
    assert ovf == 1, "drop-during-consume lost the flag"
    assert read_sig(dut.drop_cnt_out, 16) == 3, "drop count != 2+1"
    await reset_dut(dut)
    assert read_sig(dut.overflow_out, 1) == 0, "reset did not clear flag"
    assert read_sig(dut.occ_out, 5) == 0
