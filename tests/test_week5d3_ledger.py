"""Week 5d3 lead tests: writer invariants and verifier rule set.

These are the "done when" tests for the ledger pipeline on synthetic
material. The RTL-fixture tamper suite (test_week5d3_tamper.py) leans on
the same rules; here each rule is isolated via re-minted white-box rows so
the FIRST verdict is the interesting one (Amendment 1 collects every
failure, so a row can fail two checks and we assert both).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from edge.secure_element import SecureElement, SEError
from ledger.verifier import (
    _ORDER,
    LEDGER_SCHEMA,
    M_MAX,
    M_MIN,
    Anchor,
    LedgerVerifier,
    classify,
)
from ledger.writer import WriterError, write_ledger
from sim.provision import derive_keys
from tests.week5d3_support import (
    HASH_OVERFLOW_BIT,
    re_mint,
    signed_chain,
)

SEED = 42


def verify(records, expected_device_id=1, anchor=None, check_zc_range=True):
    km = derive_keys(SEED)
    return LedgerVerifier(
        km.verify_key,
        expected_device_id=expected_device_id,
        anchor=anchor,
        check_zc_range=check_zc_range,
    ).verify(records)


# ---------------------------------------------------------------- writer


def test_write_read_roundtrip_chain_intact(tmp_path):
    records = signed_chain(3)
    se = SecureElement.from_seed(SEED)
    path = tmp_path / "ledger.jsonl"
    n = write_ledger(records, path, se, device_id=1)
    assert n == 3
    from ledger.writer import read_ledger

    loaded = read_ledger(path)
    assert [r["counter"] for r in loaded] == [0, 1, 2]
    assert loaded[0]["prev_hash_hex"] == bytes(32).hex()
    assert loaded[1]["prev_hash_hex"] == records[0]["window_hash_hex"]
    ver = LedgerVerifier(derive_keys(SEED).verify_key).verify(loaded)
    assert ver.counts["valid"] == 3 and ver.counts["rejected"] == 0
    assert ver.accepted_for_issuance and ver.unverified_tail


def test_write_refuses_hmac_mismatch(tmp_path):
    records = signed_chain(1)
    hmac_bytes = bytes.fromhex(records[0]["hmac_hex"])
    corrupted = hmac_bytes[:-1] + bytes([hmac_bytes[-1] ^ 0xFF])
    records[0]["hmac_hex"] = corrupted.hex()
    with pytest.raises(SEError):
        write_ledger(records, tmp_path / "x.jsonl", SecureElement.from_seed(SEED), device_id=1)


def test_write_refuses_chain_break(tmp_path):
    records = signed_chain(3)
    records[1]["prev_hash_hex"] = bytes(32).hex()
    with pytest.raises(WriterError, match="chain break"):
        write_ledger(records, tmp_path / "x.jsonl", SecureElement.from_seed(SEED), device_id=1)


def test_write_refuses_descriptor_mismatch(tmp_path):
    records = signed_chain(1)
    records[0]["p_avg_q30"] += 1  # field no longer matches descriptor_hex
    with pytest.raises(WriterError, match="round trip"):
        write_ledger(records, tmp_path / "x.jsonl", SecureElement.from_seed(SEED), device_id=1)


# ---------------------------------------------------------------- classify


def test_classify_classes():
    assert classify({"window_flags": 0x1F}) == "valid"
    assert classify({"window_flags": 0x1F | (1 << 5)}) == "excluded"  # NEG clamp
    assert classify({"window_flags": 0x00}) == "excluded"  # all-invalid
    assert classify({"window_flags": 0x1F}, ("bad-signature",)) == "rejected"
    assert classify({"window_flags": 0x1F | (1 << HASH_OVERFLOW_BIT)}) == "rejected"


def test_classify_importable_from_clean_interpreter():
    code = (
        "from ledger.verifier import classify, LEDGER_SCHEMA;"
        "r={'window_flags': 0x1F|(1<<7)};"
        "assert classify(r)=='rejected';"
        "assert classify({'window_flags':0x1F})=='valid';"
        "print(LEDGER_SCHEMA)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).parents[1]
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == LEDGER_SCHEMA


# ---------------------------------------------------------------- keyless / pubkey-only


def test_verify_uses_only_public_key_material():
    rec = signed_chain(1)
    km = derive_keys(SEED)
    ver = LedgerVerifier(km.verify_key)
    # assert no secret material reachable / needed
    attrs = ("hmac" in a or "secret" in a for a in dir(ver) if not a.startswith("_"))
    assert not any(attrs)
    assert ver.verify(rec).counts["valid"] == 1


def test_wrong_pubkey_fails_every_signature():
    records = signed_chain(3)
    wrong = derive_keys(1234).verify_key
    ver = LedgerVerifier(wrong).verify(records)
    assert ver.counts["rejected"] == 3
    assert all(set(o.failures) == {"bad-signature"} for o in ver.outcomes)


# ---------------------------------------------------------------- per-rule verdicts


def test_bad_device_id():
    rec = signed_chain(1, device_id=7)
    ver = LedgerVerifier(derive_keys(SEED).verify_key, expected_device_id=1).verify(rec)
    o = ver.outcomes[0]
    assert o.first_verdict == "bad-device-id" and set(o.failures) == {"bad-device-id"}


def test_bad_descriptor_edited_field_without_resign():
    rec = signed_chain(1)
    rec[0]["energy_uwh"] += 1  # convenience field edited, signed descriptor untouched
    o = verify(rec).outcomes[0]
    assert o.first_verdict == "bad-descriptor" and set(o.failures) == {"bad-descriptor"}


def test_bad_overrun_flag_count_mismatch_both_directions():
    # count>0 with bit 6 clear
    rec = signed_chain(2)
    rec[0] = re_mint(rec[0], overrun_cnt=1)
    rec[0]["window_flags"] &= ~(1 << 6)
    rec[0] = re_mint(rec[0])
    ver = verify(rec)
    assert ver.outcomes[0].first_verdict == "bad-overrun"
    # count==0 with bit 6 set
    rec = signed_chain(2)
    rec[0] = re_mint(rec[0], window_flags=rec[0]["window_flags"] | (1 << 6))
    assert verify(rec).outcomes[0].first_verdict == "bad-overrun"


def test_bad_overrun_range_count_over_five():
    rec = signed_chain(2)
    rec[0] = re_mint(
        rec[0], overrun_cnt=6, window_flags=rec[0]["window_flags"] | (1 << 6)
    )  # bit 6 consistent; only the count exceeds the receiver's rule (max 5)
    assert verify(rec).outcomes[0].first_verdict == "bad-overrun-range"


def test_bad_bit7_only_check_skips_field_level():
    rec = signed_chain(2)
    rec[0] = re_mint(rec[0], window_flags=rec[0]["window_flags"] | (1 << HASH_OVERFLOW_BIT))
    o = verify(rec).outcomes[0]
    assert o.first_verdict == "bad-bit7" and set(o.failures) == {"bad-bit7"}


def test_bad_chain_deleted_middle_record():
    rec = signed_chain(4)
    tampered = [rec[0], rec[2], rec[3]]
    ver = verify(tampered)
    o = ver.outcomes[1]  # former record 2 now sits behind record 0's tail
    assert o.first_verdict == "bad-chain" and "bad-chain" in o.failures


def test_bad_genesis_head_truncation():
    rec = signed_chain(4)
    tampered = rec[1:]
    ver = verify(tampered)
    assert ver.outcomes[0].first_verdict == "bad-genesis"  # counter 1, no genesis link


def test_bad_genesis_forged_prev_on_first_row():
    rec = signed_chain(1)
    rec[0]["prev_hash_hex"] = bytes.fromhex("AB" * 32).hex()
    o = verify(rec).outcomes[0]
    assert o.first_verdict == "bad-genesis"


def test_second_genesis_is_a_reset_event_and_not_issuable():
    rec = signed_chain(3)
    # forged record: counter 0 + genesis prev, re-minted so its own sig is valid.
    # It starts a NEW chain segment after the true tail: reset event + chain break.
    forged = re_mint(rec[0], counter=0)
    forged["prev_hash_hex"] = bytes(32).hex()
    forged["window_flags"] = 0x1F
    rec.append(forged)
    ver = verify(rec)
    o = ver.outcomes[-1]
    assert o.first_verdict == "bad-genesis" and "bad-chain" in o.failures
    assert ver.reset_events == 1 and not ver.accepted_for_issuance


def test_bad_replay_reminated_alias_with_patched_chain():
    rec = signed_chain(4)
    old = rec[1]
    alias = re_mint(old, counter=1)
    alias["prev_hash_hex"] = rec[-1]["window_hash_hex"]  # patch chain: replay, not break
    rec.append(alias)
    o = verify(rec).outcomes[-1]
    assert o.first_verdict == "bad-replay" and "bad-continuity" in o.failures


def test_duplicate_row_first_chain_break_second_replay():
    rec = signed_chain(4)
    rec.append(dict(rec[1]))  # verbatim copy, chain pointer now stale
    o = verify(rec).outcomes[-1]
    assert o.first_verdict == "bad-chain" and "bad-replay" in o.failures


def test_bad_continuity_counter_jumps_forward():
    rec = signed_chain(4)
    rec[2] = re_mint(rec[2], counter=5)
    # the jump is its own crime; the next row's prev is still consistent
    o = verify(rec).outcomes[2]
    assert o.first_verdict == "bad-continuity" and "bad-continuity" in o.failures


def test_bad_gap_unattributed_jump_bit6_clear():
    rec = signed_chain(3)
    rec[1] = re_mint(rec[1], window_start=int(rec[1]["window_start"]) + 500)
    ver = verify(rec)
    o = ver.outcomes[2]  # the LATER record pays for the unattributed jump
    assert o.first_verdict == "bad-gap"


def test_gap_bit6_drop_below_min_span_fails_but_boundary_passes():
    # gap 0 with a claimed overrun of 1: 0 < 1*11
    rec = signed_chain(2, overruns=[0, 0])
    rec[0] = re_mint(rec[0], overrun_cnt=1)
    rec[0]["window_flags"] = rec[0]["window_flags"] | (1 << 6)
    rec[0] = re_mint(rec[0])
    assert verify(rec).outcomes[1].first_verdict == "bad-gap"
    # boundary gap == count*11 must pass (also the `>=` vs `>` mutation catcher)
    rec = signed_chain(3, overruns=[2, 0, 0])
    assert verify(rec).counts["valid"] == 3


def test_bad_invalid_count():
    rec = signed_chain(2)
    rec[0] = re_mint(
        rec[0], zc_samples=[2000, 2000, 2000, 2000, 2000], window_flags=0x1F & ~0x04
    )  # slot 2 invalid but zc 2000
    o = verify(rec).outcomes[0]
    assert o.first_verdict == "bad-invalid-count" and "bad-invalid-count" in o.failures


def test_bad_neg_clamp():
    rec = signed_chain(2)
    rec[0] = re_mint(rec[0], window_flags=0x1F | (1 << 5))  # claims NEG but energy != 0
    assert verify(rec).outcomes[0].first_verdict == "bad-neg-clamp"


def test_bad_range_and_boundaries():
    # Re-minting zc changes the attested span, so the paired slot offsets the
    # sum to keep the next record's window_start consistent (gap stays 0).
    lo_bad, hi_bad = M_MIN - 1, M_MAX + 1
    for m in (lo_bad, hi_bad):
        rec = signed_chain(2)
        rec[0] = re_mint(rec[0], zc_samples=[m, 4000 - m, 2000, 2000, 2000])
        assert verify(rec).outcomes[0].first_verdict == "bad-range"
    rec = signed_chain(2)
    rec[0] = re_mint(rec[0], zc_samples=[M_MIN, 4000 - M_MIN, 2000, 2000, 2000])
    assert verify(rec).counts["valid"] == 2


def test_bad_range_skipped_for_bit7_records():
    rec = signed_chain(2)
    rec[0] = re_mint(
        rec[0],
        zc_samples=[99, 2000, 2000, 2000, 2000],
        window_flags=0x1F | (1 << HASH_OVERFLOW_BIT),
    )
    o = verify(rec).outcomes[0]
    assert o.first_verdict == "bad-bit7" and set(o.failures) == {"bad-bit7"}


def test_bad_signature_swapped():
    rec = signed_chain(2)
    rec[0]["sig_hex"], rec[1]["sig_hex"] = rec[1]["sig_hex"], rec[0]["sig_hex"]
    o = verify(rec).outcomes[0]
    assert o.first_verdict == "bad-signature" and "bad-signature" in o.failures


def test_malformed_line_breaks_timeline_and_recovers():
    records = signed_chain(3)
    rec = dict(records[1], counter="not-a-counter")
    ver = verify([records[0], rec, records[2]])
    assert ver.outcomes[0].first_verdict == "ok"
    assert ver.outcomes[1].first_verdict == "malformed"
    # recovery is CONSERVATIVE: record 2's prev does not match the last known
    # good hash (record 1 could have hidden anything), so it fails the chain.
    assert ver.outcomes[2].first_verdict == "bad-chain"


def test_first_failure_and_all_failures_collection():
    rec = signed_chain(3)
    rec[1] = re_mint(rec[1], counter=7, device_id=9)  # two independent crimes
    o = verify(rec).outcomes[1]
    assert o.first_verdict == "bad-device-id"  # earlier in canonical order
    assert o.failures == ("bad-device-id", "bad-continuity")


# ---------------------------------------------------------------- anchor / tail


def test_no_anchor_tail_unverified():
    ver = verify(signed_chain(3))
    assert ver.unverified_tail and ver.accepted_for_issuance and ver.counts["valid"] == 3


def test_anchor_ahead_detects_tail_truncation():
    rec = signed_chain(3)  # counters 0..2
    head = rec[-1]["window_hash_hex"]
    ver = verify(rec, anchor=Anchor(expected_last_counter=3, head_hash=bytes.fromhex(head)))
    assert ver.tail_truncated and not ver.accepted_for_issuance


def test_anchor_behind_detects_overshoot():
    rec = signed_chain(3)  # counters 0..2
    ver = verify(rec, anchor=Anchor(expected_last_counter=1))
    assert ver.anchor_mismatch and not ver.accepted_for_issuance


def test_anchor_exact_match_accepts_and_verifies_head_hash():
    rec = signed_chain(3)  # counters 0..2
    head = rec[-1]["window_hash_hex"]
    ver = verify(rec, anchor=Anchor(expected_last_counter=2, head_hash=bytes.fromhex(head)))
    assert not ver.tail_truncated and not ver.anchor_mismatch and ver.accepted_for_issuance
    wrong = verify(rec, anchor=Anchor(expected_last_counter=2, head_hash=bytes(32)))
    assert wrong.anchor_mismatch and not wrong.accepted_for_issuance


# ---------------------------------------------------------------- threat model note


def test_canonical_order_is_stable_and_documented():
    assert "bad-signature" in _ORDER and "malformed" == _ORDER[0]
    assert len(set(_ORDER)) == len(_ORDER)
