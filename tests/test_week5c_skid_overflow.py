"""Week 5c3: 17-deep skid-overflow bound (fail-loud, nothing silently lost).

The 5d chain feed skid is depth 16 (see DECISIONS.md week5c3). The live
model is ``tb.w5_hmac_cocotb.skid_put`` (bound ASSERTED, not enforced):
16 queues fine, the 17th must raise — never a silent drop. The DUT-level
equivalent (valid && !ready while sealed/overflowing) is pinned by
``test_hash_overflow``, ``test_hash_sealed_force`` and
``test_hmac_overflow_abort`` (sticky ``overflow_out`` + intact digest);
this file pins the Python-model half: the bound triggers AND the queued
16 drain in order (digest-checked), so no byte is lost silently.
"""

import hashlib

import pytest

from tb.w5_hmac_cocotb import SKID_DEPTH, skid_put


def test_skid_depth_is_16():
    assert SKID_DEPTH == 16


def test_skid_17th_raises_and_16_survive_in_order():
    skid: list[int] = []
    payload = bytes(range(16))
    for b in payload:
        skid_put(skid, b, "fill-16")
    assert len(skid) == 16
    with pytest.raises(AssertionError, match="skid overflowed 16"):
        skid_put(skid, 0xAA, "17th-must-fail")
    # Nothing silently lost: the 16 queued bytes are intact and ordered.
    assert bytes(skid) == payload
    assert hashlib.sha256(bytes(skid)).digest() == hashlib.sha256(payload).digest()
    # Draining preserves order (FIFO, not a drop).
    drained = bytes(skid.pop(0) for _ in range(16))
    assert drained == payload
    assert not skid
