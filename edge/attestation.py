"""Window aggregation: Python golden model of the FPGA hash/HMAC path (D-02).

`build_record` packs 5 consecutive zero-crossing sub-windows (valid or
flagged-invalid) into one 1 Hz attestation record: descriptor bytes,
`WINDOW_HASH = SHA256(prev_hash || samples || descriptor)` over ALL
samples of all 5 sub-windows (descriptor LAST: P_AVG/ENERGY are known only
after the last sample, so descriptor-first cannot stream — Week 5c reorder),
and `HMAC-SHA256(hmac_key, descriptor || window_hash)`. It returns the
unsigned record plus the exact signing
inputs; the `SecureElement` alone turns those into a signature.

Energy/power encodings (Week 3 byte-layout note, canonical integer forms
from sensors.windows since Week 4a): `P_inst_q30 = v_q15 · i_q15` (exact,
full-scale 50 kW); `P_AVG` is the round-half-away mean over valid samples
only — the SOLE descriptor P_AVG source (Week 4b2 decision: the verifier
re-derives it without LUT knowledge; the RTL LUT mean is advisory only);
`ENERGY_UWH` accumulates integer-exact micro-watt-hours over valid
sub-windows only (`E += div_round_half_away(p_sum · 12500, 9 · 2^30)`).
Invalid sub-windows contribute samples to the hash but zero energy and
are excluded from `P_AVG` — flagged, never healed.
Negative-energy policy (Week 4a2): a record whose energy total would go
negative (possible only on synthetic/adversarial data — real motor
windows average ~+10^8 Q30) is CLAMPED to `ENERGY_UWH = 0` with the
`NEG_ENERGY` bit set in `window_flags`; `P_AVG` stays signed-exact.
Fail-loud was rejected: a Python exception has no RTL equivalent and one
bad window must never halt attestation (DoS). Week 5 verifier: on
NEG_ENERGY, exclude the record's energy from savings and flag it.
"""

import hashlib
import hmac as hmac_mod
import struct
from dataclasses import dataclass

import numpy as np

from sensors.windows import energy_uwh_increment, mean_q30_half_away

N_SUBWINDOWS = 5  # [count] ZC sub-windows per 1 Hz attestation record (D-05)
NEG_ENERGY_BIT = 5  # [bit] window_flags bit set when ENERGY_UWH is clamped at 0
OVERRUN_BIT = 6  # [bit] window_flags bit set when overrun_cnt > 0 (Week 5b)
GENESIS_HASH32 = bytes(32)  # [bytes] prev_hash before the first record
P_FS_W = 500.0 * 100.0  # [W] Q30 power full-scale (V_FS · I_FS)
Q30 = 1 << 30  # [counts] Q30 scale factor


@dataclass
class SubWindow:
    """One zero-crossing sub-window of sample codes (host units per field)."""

    v_q15: np.ndarray  # [counts] Q15 voltage codes over integer window bounds
    i_q15: np.ndarray  # [counts] Q15 current codes over integer window bounds
    valid: bool  # [flag] False → M recorded as 0, excluded from P_AVG/energy


def descriptor_bytes(
    counter: int,
    window_start: int,
    zc_samples: list[int] | tuple[int, ...],
    window_flags: int,
    device_id: int,
    p_avg_q30: int,
    energy_uwh: int,
    overrun_cnt: int = 0,
) -> bytes:
    """Pack the 36-byte signed descriptor (little-endian); range errors raise."""
    counts = list(zc_samples)
    if len(counts) != N_SUBWINDOWS:
        raise ValueError(f"zc_samples needs {N_SUBWINDOWS} entries")
    for name, value, lo, hi in (
        ("counter", counter, 0, 0xFFFFFFFF),
        ("window_start", window_start, 0, 0xFFFFFFFF),
        ("window_flags", window_flags, 0, 0xFF),
        ("device_id", device_id, 0, 0xFFFFFFFF),
        ("p_avg_q30", p_avg_q30, -(1 << 31), (1 << 31) - 1),
        ("energy_uwh", energy_uwh, 0, 0xFFFFFFFFFFFFFFFF),
        ("overrun_cnt", overrun_cnt, 0, 0xFF),
    ):
        if not lo <= int(value) <= hi:
            raise ValueError(f"{name} out of range: {value}")
    for k, m in enumerate(counts):
        if not 0 <= int(m) <= 0xFFFF:
            raise ValueError(f"zc_samples[{k}] out of u16 range: {m}")
    return struct.pack(
        "<II5HBBIiQ",
        int(counter),
        int(window_start),
        *[int(m) for m in counts],
        int(window_flags),
        int(overrun_cnt),  # was reserved 0x00 (Week 3); overrun count since 5b
        int(device_id),
        int(p_avg_q30),
        int(energy_uwh),
    )


def build_window_hash(prev_hash: bytes, descriptor: bytes, sub_windows: list[SubWindow]) -> bytes:
    """SHA-256 over `prev_hash || V/I/P samples || descriptor` (all sub-windows).

    Descriptor is hashed LAST (Week 5c reorder): P_AVG/ENERGY are known only
    after the last sample, so descriptor-first cannot stream — the Week 5
    wrapper feeds prev_hash, then samples as they arrive, then the 36-byte
    descriptor at window end. Same three components bound, new order; the
    SIG preimage and HMAC layouts are UNCHANGED.
    """
    if len(prev_hash) != 32 or len(descriptor) != 36:
        raise ValueError("prev_hash must be 32 bytes, descriptor 36 bytes")
    h = hashlib.sha256()
    h.update(bytes(prev_hash))
    for sub in sub_windows:
        v = np.asarray(sub.v_q15, dtype=np.int64).astype("<i2")
        i = np.asarray(sub.i_q15, dtype=np.int64).astype("<i2")
        if v.shape != i.shape:
            raise ValueError("V/I sample arrays must match per sub-window")
        p = (np.asarray(sub.v_q15, dtype=np.int64) * np.asarray(sub.i_q15, dtype=np.int64)).astype(
            "<i4"
        )
        h.update(v.tobytes())
        h.update(i.tobytes())
        h.update(p.tobytes())
    h.update(bytes(descriptor))
    return h.digest()


def compute_hmac(hmac_key: bytes, descriptor: bytes, window_hash: bytes) -> bytes:
    """HMAC-SHA256 over `descriptor || window_hash` ("header + hash", D-02)."""
    if len(hmac_key) != 32 or len(window_hash) != 32:
        raise ValueError("hmac_key and window_hash must be 32 bytes each")
    return hmac_mod.new(
        bytes(hmac_key), bytes(descriptor) + bytes(window_hash), hashlib.sha256
    ).digest()


def build_record(
    counter: int,
    window_start: int,
    sub_windows: list[SubWindow],
    device_id: int,
    prev_hash: bytes,
    energy_prev_uwh: int,
    hmac_key: bytes,
    fs_hz: float = 10_000.0,
    overrun_cnt: int = 0,
) -> tuple[dict, tuple[bytes, bytes, bytes]]:
    """Build one unsigned attestation record plus `(descriptor, hash, hmac)`.

    Returns:
        Tuple `(record, signing_inputs)`; the caller obtains
        `record["sig_hex"]` from `SecureElement.attest(*signing_inputs)`.
        `record` also carries `samples_v`/`samples_i` (lists of ints) so the
        reference receiver recomputes the hash from received data.
    """
    if len(sub_windows) != N_SUBWINDOWS:
        raise ValueError(f"need exactly {N_SUBWINDOWS} sub-windows")
    if fs_hz != 10_000.0:
        # The integer-exact energy form bakes in fs = 10 kHz (D-04); a
        # different rate needs a re-derived constant, never silent scaling.
        raise ValueError(f"energy constant valid only for fs = 10 kHz, got {fs_hz}")
    zc = [int(np.asarray(s.v_q15).shape[0]) if s.valid else 0 for s in sub_windows]
    flags = 0
    for k, s in enumerate(sub_windows):
        flags |= (1 if s.valid else 0) << k
    p_sum, p_count, energy_uwh = 0, 0, int(energy_prev_uwh)
    for s, m in zip(sub_windows, zc):
        if not s.valid:
            continue
        v = np.asarray(s.v_q15, dtype=np.int64)
        i = np.asarray(s.i_q15, dtype=np.int64)
        p = v * i  # [Q30 counts] exact per-sample power
        p_sum += int(p.sum())
        p_count += int(p.shape[0])
        energy_uwh += energy_uwh_increment(int(p.sum()), m)  # [µWh] integer-exact
    if energy_uwh < 0:  # NEG_ENERGY policy: clamp at 0 + flag; never wrap/raise
        energy_uwh = 0
        flags |= 1 << NEG_ENERGY_BIT
    overrun_cnt = int(overrun_cnt)
    if overrun_cnt < 0:
        raise ValueError(f"overrun_cnt out of range: {overrun_cnt}")
    overrun_cnt = min(overrun_cnt, 0xFF)  # u8-saturating (255 reads as ">= 255")
    if overrun_cnt > 0:  # OVERRUN rule: count > 0 IFF bit 6 (Week 5b)
        flags |= 1 << OVERRUN_BIT
    neg_clamped = bool((flags >> NEG_ENERGY_BIT) & 1)
    p_avg_q30 = mean_q30_half_away(p_sum, p_count) if p_count else 0
    descriptor = descriptor_bytes(
        counter, window_start, zc, flags, device_id, p_avg_q30, energy_uwh, overrun_cnt
    )
    window_hash = build_window_hash(bytes(prev_hash), descriptor, sub_windows)
    hmac_tag = compute_hmac(hmac_key, descriptor, window_hash)
    record = {
        "counter": int(counter),
        "window_start": int(window_start),
        "zc_samples": zc,
        "zc_samples_raw": struct.pack("<5H", *zc),
        "window_flags": flags,
        "device_id": int(device_id),
        "p_avg_q30": p_avg_q30,
        "energy_uwh": energy_uwh,
        "neg_energy_clamped": neg_clamped,  # [flag] metadata mirror of flags bit 5
        "overrun_cnt": overrun_cnt,  # [counts] u8-saturating drop count (bit 6 iff > 0)
        "prev_hash_hex": bytes(prev_hash).hex(),
        "window_hash_hex": window_hash.hex(),
        "hmac_hex": hmac_tag.hex(),
        "descriptor_hex": descriptor.hex(),
        "samples_v": [[int(x) for x in np.asarray(s.v_q15, dtype=np.int64)] for s in sub_windows],
        "samples_i": [[int(x) for x in np.asarray(s.i_q15, dtype=np.int64)] for s in sub_windows],
    }
    return record, (descriptor, window_hash, hmac_tag)
