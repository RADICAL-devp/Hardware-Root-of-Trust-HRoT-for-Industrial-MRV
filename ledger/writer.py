"""Hash-chained JSONL ledger writer (Week 5d3).

Trust boundary (D-02): the secure element holds the Ed25519 private key
and signs at the source; the FPGA publishes descriptor + window hash +
HMAC. This writer is the seam between the two: it re-packs the descriptor
from the published fields, verifies the 36-byte round trip, hands the
(descriptor, window_hash, hmac) triple to `SecureElement.attest` — which
REFUSES to sign an HMAC mismatch — and appends ``SIG = Ed25519(HMAC ||
descriptor || window_hash)`` to the ledger line.

Chain provenance: the ledger is a hash chain. Record 0 hashes
``prev_hash = 0x00...00`` (genesis); every later record's prev_hash MUST
equal the previous record's window_hash. The writer derives the chain from
the written window hashes and refuses to emit a record that disagrees
(gap/deletion/reorder breaks the chain at write time, mirroring what the
verifier detects at read time).

Output schema (one compact JSON object per line):

``{schema, device_id, counter, window_start, zc_samples[5], window_flags,
overrun_cnt, p_avg_q30, energy_uwh, prev_hash_hex, window_hash_hex,
hmac_hex, descriptor_hex, sig_hex, telemetry, telemetry_attested}``

Telemetry is carried verbatim per record counter and is never part of the
HMAC/SIG: the JSONL marks ``telemetry_attested: false`` so a downstream
consumer cannot mistake untrusted diagnostics for attested data (the
capability matrix documents this exclusion).
"""

import json
from pathlib import Path

from edge.attestation import (
    GENESIS_HASH32,
    N_SUBWINDOWS,
    descriptor_bytes,
)
from edge.secure_element import SecureElement

LEDGER_SCHEMA = "hrot-ledger-v1"

LEDGER_KEYS = (
    "counter",
    "window_start",
    "zc_samples",
    "window_flags",
    "overrun_cnt",
    "p_avg_q30",
    "energy_uwh",
    "prev_hash_hex",
    "window_hash_hex",
    "hmac_hex",
    "descriptor_hex",
    "sig_hex",
    "telemetry",
    "telemetry_attested",
)


class WriterError(Exception):
    """Ledger writer refusal: malformed input, descriptor mismatch, chain break."""


def _as_bytes(value, name: str) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = bytes.fromhex(value)
    else:
        raise WriterError(f"{name} must be bytes or hex str")
    return raw


def write_ledger(
    records,
    out_path: str | Path,
    se: SecureElement,
    device_id: int,
    telemetry: dict[int, dict] | None = None,
    prev: bytes | None = None,
) -> int:
    """Sign and write one JSONL line per record; return the record count.

    Each input record (dict) carries the descriptor fields plus
    ``window_hash`` and ``hmac`` (bytes or hex). ``prev`` seeds the chain
    (default: genesis zeros); after the first record the writer derives
    prev from the previous record's window_hash and refuses a mismatch.
    A record may carry ``descriptor_hex`` — it must equal the re-packed
    descriptor (write-time tamper guard). Telemetry is attached per
    record counter and never signed.
    """
    telemetry = telemetry or {}
    out_path = Path(out_path)
    chain_prev = GENESIS_HASH32 if prev is None else _as_bytes(prev, "prev")
    written = 0
    with open(out_path, "w") as fh:
        for rec in records:
            for key in (
                "counter",
                "window_start",
                "window_flags",
                "overrun_cnt",
                "p_avg_q30",
                "energy_uwh",
            ):
                if key not in rec:
                    raise WriterError(f"record missing field {key!r}")
            if "zc_samples" not in rec or len(rec["zc_samples"]) != N_SUBWINDOWS:
                raise WriterError("record needs exactly 5 zc_samples")
            desc = descriptor_bytes(
                int(rec["counter"]),
                int(rec["window_start"]),
                [int(m) for m in rec["zc_samples"]],
                int(rec["window_flags"]),
                int(device_id),
                int(rec["p_avg_q30"]),
                int(rec["energy_uwh"]),
                int(rec.get("overrun_cnt", 0)),
            )
            if rec.get("descriptor_hex") is not None:
                supplied = _as_bytes(rec["descriptor_hex"], "descriptor_hex")
                if supplied != desc:
                    raise WriterError(
                        "descriptor round trip failed: fields do not match descriptor_hex"
                    )
            window_hash = _as_bytes(
                rec.get("window_hash", rec.get("window_hash_hex")), "window_hash"
            )
            hmac_tag = _as_bytes(rec.get("hmac", rec.get("hmac_hex")), "hmac")
            if len(window_hash) != 32 or len(hmac_tag) != 32:
                raise WriterError("window_hash and hmac must be 32 bytes")
            if rec.get("prev_hash_hex") is not None:
                supplied_prev = _as_bytes(rec["prev_hash_hex"], "prev_hash_hex")
                if supplied_prev != chain_prev:
                    raise WriterError("chain break: supplied prev_hash != previous window_hash")
            sig = se.attest(desc, window_hash, hmac_tag)  # SEError on HMAC mismatch
            counter = int(rec["counter"])
            line = {
                "schema": LEDGER_SCHEMA,
                "device_id": int(device_id),
                "counter": counter,
                "window_start": int(rec["window_start"]),
                "zc_samples": [int(m) for m in rec["zc_samples"]],
                "window_flags": int(rec["window_flags"]),
                "overrun_cnt": int(rec.get("overrun_cnt", 0)),
                "p_avg_q30": int(rec["p_avg_q30"]),
                "energy_uwh": int(rec["energy_uwh"]),
                "prev_hash_hex": chain_prev.hex(),
                "window_hash_hex": window_hash.hex(),
                "hmac_hex": hmac_tag.hex(),
                "descriptor_hex": desc.hex(),
                "sig_hex": sig.hex(),
                "telemetry": telemetry.get(counter),
                "telemetry_attested": False,
            }
            fh.write(json.dumps(line) + "\n")
            written += 1
            chain_prev = window_hash
    return written


def read_ledger(path: str | Path) -> list[dict]:
    """Parse a JSONL ledger into a list of record dicts (schema-aware)."""
    lines = []
    with open(path) as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WriterError(f"line {lineno}: not JSON: {exc.pos}") from exc
            for key in LEDGER_KEYS:
                rec.setdefault(key, None)
            lines.append(rec)
    return lines
