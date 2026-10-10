"""Week 5d3 fixtures: committed RTL ledger files must be shippable oracles.

The committed files in tests/fixtures/ are produced by
tb/w5d_ledger_cocotb.py and are the source of truth for the tamper suite.
Every run of the generators regenerates the ledger to results/ and
byte-compares against these files (deterministic sim + writer). This
(Fast) test pins their SHA-256 so drift is caught even without the
15-minute sim, and asserts the verifier's contract on them:

- clean fixture: 4 records, all valid, counters 0..3, in-range zc, gap 0,
  chain intact, nothing rejected; the ONLY way to be accepted.
- overflow fixture: 2 records, bit 7 set, zc detection intact, both reject
  first as bad-bit7 (policy: an overflow record is never issuable).
"""

import hashlib
import json

from edge.attestation import descriptor_bytes
from ledger.verifier import (
    HASH_OVERFLOW_BIT,
    LEDGER_SCHEMA,
    M_MAX,
    M_MIN,
    LedgerVerifier,
)
from ledger.writer import read_ledger
from sim.provision import derive_keys

FIXTURES = "tests/fixtures"

PINS = {
    "ledger_rtl_clean.jsonl": "609a5996a0929fd7a2eda4ab9a77eba62905797f20a46b2b0af294ed64aa302f",
    "ledger_rtl_overflow.jsonl": "348980e3894f37f0113c1a6add55be24495f0a7a28cddf450c607d21a7fa96c8",
}

SEED = 42


def _loader(name):
    path = f"{FIXTURES}/{name}"
    raw = open(path, "rb").read()
    assert hashlib.sha256(raw).hexdigest() == PINS[name], (
        f"{name} drifted from its committed byte oracle"
    )
    return raw


def test_fixtures_are_byte_pinned():
    for name, pin in PINS.items():
        data = open(f"{FIXTURES}/{name}", "rb").read()
        assert hashlib.sha256(data).hexdigest() == pin, f"{name} drifted"


def test_clean_fixture_verifies_valid_end_to_end():
    rows = read_ledger(f"{FIXTURES}/ledger_rtl_clean.jsonl")
    assert len(rows) == 4
    assert [r["counter"] for r in rows] == [0, 1, 2, 3]
    assert rows[0]["prev_hash_hex"] == bytes(32).hex()
    for k, r in enumerate(rows):
        assert r["schema"] == LEDGER_SCHEMA
        assert r["device_id"] == 1
        assert r["telemetry_attested"] is False
        if k:
            assert r["prev_hash_hex"] == rows[k - 1]["window_hash_hex"]
        assert all(M_MIN <= m <= M_MAX for m in r["zc_samples"])
    ver = LedgerVerifier(derive_keys(SEED).verify_key).verify(rows)
    assert ver.counts == {"valid": 4, "excluded": 0, "rejected": 0}
    assert ver.reset_events == 0 and ver.accepted_for_issuance and ver.unverified_tail


def test_overflow_fixture_rejects_only_as_bit7():
    rows = read_ledger(f"{FIXTURES}/ledger_rtl_overflow.jsonl")
    assert len(rows) == 2
    for k, r in enumerate(rows):
        assert r["window_flags"] & (1 << HASH_OVERFLOW_BIT)
        assert r["window_flags"] & 0x1F == 0  # valid bits cleared
        # detection stayed intact: zc == golden span, inside the range window
        assert all(M_MIN <= m <= M_MAX for m in r["zc_samples"])
        assert r["p_avg_q30"] == 0
    ver = LedgerVerifier(derive_keys(SEED).verify_key).verify(rows)
    assert ver.counts == {"valid": 0, "excluded": 0, "rejected": 2}
    assert all(
        o.first_verdict == "bad-bit7" and set(o.failures) == {"bad-bit7"} for o in ver.outcomes
    )


def test_fixtures_descriptors_repack_exactly():
    for name in PINS:
        for line in open(f"{FIXTURES}/{name}"):
            r = json.loads(line)
            packed = descriptor_bytes(
                r["counter"],
                r["window_start"],
                r["zc_samples"],
                r["window_flags"],
                r["device_id"],
                r["p_avg_q30"],
                r["energy_uwh"],
                r["overrun_cnt"],
            )
            assert packed.hex() == r["descriptor_hex"], (name, r["counter"])
