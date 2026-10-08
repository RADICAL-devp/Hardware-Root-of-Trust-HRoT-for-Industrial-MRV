"""Week 5b: overrun descriptor plumbing (edge/attestation.py + receiver.py).

The OVERRUN rule (count > 0 IFF flags bit 6, DECISIONS.md Week 5b):
builder derives bit 6 from the count (u8-saturating); the receiver
rejects both mismatch directions as bad-overrun before touching keys;
all 256 count encodings verify-accept while every post-signing flip of
the count byte rejects. Sub-windows are tiny on purpose (fast hashes).
"""

import numpy as np
import pytest

from edge.attestation import OVERRUN_BIT, SubWindow, build_record, descriptor_bytes
from edge.receiver import RecordReceiver
from edge.secure_element import SecureElement
from sim.provision import derive_keys

HMAC_KEY = derive_keys(42).hmac_key
SE = SecureElement.from_seed(42)
VK = SecureElement.from_seed(42).public_key

OVERRUN_BYTE = 19  # [bytes] offset of overrun_cnt in the 36-byte descriptor
OVERRUN_HEX = slice(2 * OVERRUN_BYTE, 2 * OVERRUN_BYTE + 2)


def tiny_subs():
    """Five valid 8-sample sub-windows (deterministic)."""
    return [
        SubWindow(
            v_q15=np.array([1000] * 8, dtype=np.int64),
            i_q15=np.array([500] * 8, dtype=np.int64),
            valid=True,
        )
        for _ in range(5)
    ]


def signed_record(overrun_cnt: int) -> dict:
    """Build + attest one record carrying overrun_cnt [counts]."""
    rec, (desc, wh, hmac) = build_record(
        counter=7,
        window_start=70000,
        sub_windows=tiny_subs(),
        device_id=1,
        prev_hash=bytes(32),
        energy_prev_uwh=0,
        hmac_key=HMAC_KEY,
        overrun_cnt=overrun_cnt,
    )
    rec["sig_hex"] = SE.attest(desc, wh, hmac).hex()
    return rec


def receiver() -> RecordReceiver:
    return RecordReceiver(VK, HMAC_KEY)


def test_builder_derives_bit6_and_clamps():
    for count, exp_count, exp_bit in ((0, 0, 0), (1, 1, 1), (5, 5, 1), (255, 255, 1)):
        rec = signed_record(count)
        assert rec["overrun_cnt"] == exp_count
        assert ((rec["window_flags"] >> OVERRUN_BIT) & 1) == exp_bit
        desc = bytes.fromhex(rec["descriptor_hex"])
        assert len(desc) == 36
        assert desc[OVERRUN_BYTE] == exp_count, "count byte sits at offset 19"
    rec = signed_record(300)
    assert rec["overrun_cnt"] == 255  # u8-saturating reads as ">= 255"
    assert ((rec["window_flags"] >> OVERRUN_BIT) & 1) == 1
    with pytest.raises(ValueError):
        signed_record(-1)


def test_encode_sweep_all_counts_accept():
    for count in range(256):
        rec = signed_record(count)
        verdict = receiver().verify(rec)
        assert verdict.accepted, f"count={count}: {verdict.reason}"
        assert ((rec["window_flags"] >> OVERRUN_BIT) & 1) == (1 if count else 0)
    # Spot-check the rule is live, not vacuous: count 0 has bit 6 clear.


def test_count_byte_flip_sweep_rejects():
    rec = signed_record(7)
    base = rec["descriptor_hex"]
    seen = set()
    for bit in range(8):
        raw = bytearray.fromhex(base)
        raw[OVERRUN_BYTE] ^= 1 << bit
        rec["descriptor_hex"] = raw.hex()
        verdict = receiver().verify(rec)
        assert not verdict.accepted, f"bit {bit} flip accepted"
        assert verdict.reason == "bad-descriptor", verdict.reason
        seen.add(verdict.reason)
    for value in (0, 1, 255, 254):
        raw = bytearray.fromhex(base)
        raw[OVERRUN_BYTE] = value
        if raw.hex() == base:
            continue
        rec["descriptor_hex"] = raw.hex()
        verdict = receiver().verify(rec)
        assert not verdict.accepted, f"value {value} accepted"
    assert seen == {"bad-descriptor"}


def _remint_consistent(rec: dict, flags: int, overrun_cnt: int) -> dict:
    """Rebuild descriptor_hex to match hand-set fields (rule-tester)."""
    rec = dict(rec)
    rec["window_flags"] = flags
    rec["overrun_cnt"] = overrun_cnt
    rec["descriptor_hex"] = descriptor_bytes(
        rec["counter"],
        rec["window_start"],
        rec["zc_samples"],
        flags,
        rec["device_id"],
        rec["p_avg_q30"],
        rec["energy_uwh"],
        overrun_cnt,
    ).hex()
    return rec


def test_mismatch_bit6_set_count_zero_rejects():
    rec = signed_record(0)
    bad = _remint_consistent(rec, rec["window_flags"] | (1 << OVERRUN_BIT), 0)
    verdict = receiver().verify(bad)
    assert not verdict.accepted and verdict.reason == "bad-overrun", verdict


def test_mismatch_bit6_clear_count_nonzero_rejects():
    rec = signed_record(5)
    bad = _remint_consistent(rec, rec["window_flags"] & ~(1 << OVERRUN_BIT), 5)
    verdict = receiver().verify(bad)
    assert not verdict.accepted and verdict.reason == "bad-overrun", verdict


def test_clean_record_accepts():
    rec = signed_record(0)
    assert ((rec["window_flags"] >> OVERRUN_BIT) & 1) == 0
    verdict = receiver().verify(rec)
    assert verdict.accepted, verdict.reason
