"""Week 5d3 uniqueness: 10,000-record chain, golden model, seeded (Amendment 5).

WHAT "10,000" COUNTS: records (one Ed25519 attestation each), spanning
50,000 fingerprint sub-windows. At one attestation per second (5 windows,
RTL window ~20 ms) this is ~2.8 h of continuous operation — far past any
intra-day rollover concern, and 10^4 > odds of accidental collision.

Crypto uniqueness under the ledger structure (no hash-guessing feasible,
but the run PROVES the structure sustains it):

- counter 0..9_999 exactly once each,
- header/hash chain intact end to end (verified per record),
- all 10,000 Ed25519 signatures distinct,
- all 10,000 window SHA-256 hashes distinct,
- all 10,000 HMAC tags (over descriptor||hash) distinct.

WINDOW MIX (Amendment 5): valid sub-windows use 8 synthetic samples (below
the RTL window floor M_MIN=1810) so the zc-RANGE check is DISABLED for this
run (declared divergence: the range rule is exercised separately by the RTL
clean fixture, M ~= 2000 in-range, and by unit boundary tests). 1 record in
17 (~5.9%, inside the requested 5-10%) carries ONE invalid sub-window
(zc=0, valid bit clear), 1 record is ALL-invalid (energy excluded), and ~1%
carry REAL in-range window lengths (M in [1900, 2200]) — the last slice is
PROVEN compatible with the strict range check in a dedicated small chain.

Invalid sub-windows advance NO declared timeline span (the golden model's
declared chronology counts only attested samples), so every bit-6-clear gap
is exactly 0 — the RTL-layer distinction between an overrun drop (attributed
by bit 6 + count) and a natural invalid window is documented in DECISIONS.
"""

import pytest

from ledger.verifier import M_MAX, M_MIN, LedgerVerifier
from sim.provision import derive_keys
from tests.week5d3_support import signed_chain

SEED = 42
N_RECORDS = 10_000
IN_RANGE_M = 2000
TINY_M = 8


def _window_lens(i: int) -> int:
    return IN_RANGE_M if i % 97 == 0 else TINY_M


def _flags(i: int) -> int:
    if i == 5000:  # exactly one all-invalid record (energy excluded)
        return 0x00
    if i % 17 == 0:  # ~5.9% of records carry one invalid sub-window
        return 0x1F & ~(1 << (i % 5))
    return 0x1F


@pytest.fixture(scope="session")
def uniqueness_run():
    return signed_chain(N_RECORDS, seed=SEED, window_lens=_window_lens, flags=_flags)


def test_uniqueness_run_full_chain_verified(uniqueness_run):
    records = uniqueness_run
    assert len(records) == N_RECORDS
    ver = LedgerVerifier(derive_keys(SEED).verify_key, check_zc_range=False).verify(records)
    assert ver.counts["valid"] + ver.counts["excluded"] == N_RECORDS
    assert ver.counts["rejected"] == 0, ver.counts
    assert ver.reset_events == 0
    assert all(o.verified for o in ver.outcomes), [
        o.first_verdict for o in ver.outcomes if not o.verified
    ]


def test_uniqueness_excluded_classes_are_present(uniqueness_run):
    records = uniqueness_run
    ver = LedgerVerifier(derive_keys(SEED).verify_key, check_zc_range=False).verify(records)
    # Exactly one record is ALL-invalid (i == 5000) -> excluded; every other
    # record keeps >= 4 valid sub-windows so it stays VALID (partial windows
    # still attribute their energy). No NEG-clamps fire: cumulative energy is
    # positive from the first record (documented in the module docstring).
    assert ver.counts["excluded"] == 1, ver.counts
    assert ver.counts["valid"] == N_RECORDS - 1
    marked = [r["counter"] for r in records if r["window_flags"] == 0x00]
    assert marked == [5000]


def test_uniqueness_counter_exactly_once(uniqueness_run):
    assert [r["counter"] for r in uniqueness_run] == list(range(N_RECORDS))


def test_uniqueness_sigs_hashes_hmacs_all_distinct(uniqueness_run):
    records = uniqueness_run
    assert len({r["sig_hex"] for r in records}) == N_RECORDS
    assert len({r["window_hash_hex"] for r in records}) == N_RECORDS
    assert len({r["hmac_hex"] for r in records}) == N_RECORDS


def test_uniqueness_chain_hashes_bound_every_segment(uniqueness_run):
    records = uniqueness_run
    for k, r in enumerate(records[1:], start=1):
        assert r["prev_hash_hex"] == records[k - 1]["window_hash_hex"]
    a, b = 3, 7000
    assert records[b]["window_hash_hex"] != records[a]["window_hash_hex"]


def test_in_range_records_verify_with_strict_range_check():
    # A dedicated small chain of real-sized windows MUST pass the strict
    # zc-range check end to end (the RTL clean fixture is the RTL-world
    # proof; this is the golden-model proof with the SAME rule enabled).
    n = 24
    records = signed_chain(n, seed=SEED, window_lens=IN_RANGE_M, flags=0x1F)
    assert all(M_MIN <= m <= M_MAX for r in records for m in r["zc_samples"])
    ver = LedgerVerifier(derive_keys(SEED).verify_key, check_zc_range=True).verify(records)
    assert ver.counts["valid"] == n and ver.counts["rejected"] == 0
