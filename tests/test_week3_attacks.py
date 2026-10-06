"""Week 3: stream attack handling on L0 frames + wrong-key signatures.

Each test is independent: corrupted payload, every single-bit position,
replay, reorder (flag-and-accept per plan approval), drop-as-gap, and a
signature under the wrong key.
"""

import numpy as np

from edge.attestation import GENESIS_HASH32, SubWindow, build_record, descriptor_bytes
from edge.framing import FRAME_LEN, FrameDecoder, encode_frame
from edge.receiver import L0Receiver, RecordReceiver
from edge.secure_element import SecureElement
from sim.provision import derive_keys


def _stream(n: int, seed: int = 7) -> list[bytes]:
    rng = np.random.default_rng(seed)
    v = rng.integers(-32768, 32768, size=n, dtype=np.int64).astype(np.int32)
    i = rng.integers(-32768, 32768, size=n, dtype=np.int64).astype(np.int32)
    return [encode_frame(k, int(v[k]), int(i[k])) for k in range(n)]


def _accept_counters(raw_frames: list[bytes]) -> tuple[list[int], L0Receiver]:
    dec, rx = FrameDecoder(), L0Receiver()
    accepted = []
    for raw in raw_frames:
        for frame in dec.feed(raw):
            verdict = rx.ingest(frame)
            assert verdict != "rejected-replay"  # unexpected in these flows
            accepted.append(frame.counter)
    return accepted, rx


def test_corrupted_payload_byte_rejected():
    frames = _stream(4)
    bad = bytearray(frames[2])
    bad[2] ^= 0xFF  # corrupt a counter payload byte wholesale
    accepted, _ = _accept_counters([frames[0], frames[1], bytes(bad), frames[3]])
    assert accepted == [0, 1, 3]  # counter 2 never yields a valid frame


def test_single_bit_flip_at_every_position_rejected():
    victim = _stream(1)[0]
    nxt = encode_frame(1, 11, -11)
    for bit in range(8 * FRAME_LEN):  # all 88 positions, no sampling
        bad = bytearray(victim)
        bad[bit // 8] ^= 1 << (bit % 8)
        dec = FrameDecoder()
        got = dec.feed(bytes(bad)) + dec.feed(nxt)
        assert [f.counter for f in got] == [1], f"bit {bit} slipped through"


def test_replayed_frame_rejected():
    frames = _stream(10)
    dec, rx = FrameDecoder(), L0Receiver()
    for raw in frames:
        for frame in dec.feed(raw):
            assert rx.ingest(frame) in ("ok", "ok-gap")
    # Adversary re-injects an old valid frame: CRC passes, counter must not.
    replayed = list(dec.feed(frames[3]))
    assert len(replayed) == 1
    assert rx.ingest(replayed[0]) == "rejected-replay"
    assert sorted(rx.seen) == list(range(10))


def test_reordered_frames_rejected_and_recorded_as_gap():
    frames = _stream(5)
    dec, rx = FrameDecoder(), L0Receiver()
    order = [frames[0], frames[1], frames[3], frames[2], frames[4]]
    verdicts = [rx.ingest(f) for raw in order for f in dec.feed(raw)]
    assert verdicts == ["ok", "ok", "ok-gap", "rejected-reorder", "ok"]
    assert sorted(rx.seen) == [0, 1, 3, 4]  # late frame dropped, not absorbed ...
    assert rx.rejected_reorders == [2]  # ... counted as rejected ...
    assert rx.missing() == [2]  # ... and recorded as a gap, never silent.


def test_replay_and_reorder_verdicts_differ():
    frames = _stream(5)
    dec, rx = FrameDecoder(), L0Receiver()
    for raw in (frames[0], frames[1], frames[3]):
        for frame in dec.feed(raw):
            assert rx.ingest(frame) in ("ok", "ok-gap")
    replayed = list(dec.feed(frames[1]))  # duplicate of an accepted counter ...
    assert rx.ingest(replayed[0]) == "rejected-replay"
    late = list(dec.feed(frames[2]))  # ... vs unseen counter below the watermark.
    assert rx.ingest(late[0]) == "rejected-reorder"
    assert rx.rejected_reorders == [2] and 2 not in rx.seen
    assert rx.missing() == [2]


def test_dropped_frame_detected_as_gap():
    frames = _stream(10)
    kept = [f for k, f in enumerate(frames) if k != 5]  # frame 5 lost on the wire
    accepted, rx = _accept_counters(kept)
    assert accepted == [0, 1, 2, 3, 4, 6, 7, 8, 9]
    assert rx.missing() == [5]  # detected as a counter gap ...
    assert rx.gaps == [(5, 5)]  # ... not silently accepted.


def _one_signed_record(sign_seed: int):
    km = derive_keys(sign_seed)
    se = SecureElement.from_key_material(km)
    n = 200
    v = np.full(n, 20000, dtype=np.int64)
    i = np.full(n, 8000, dtype=np.int64)
    subs = [SubWindow(v_q15=v, i_q15=i, valid=True) for _ in range(5)]
    record, _ = build_record(
        counter=41,
        window_start=41 * 1000,
        sub_windows=subs,
        device_id=km.device_id,
        prev_hash=GENESIS_HASH32,
        energy_prev_uwh=0,
        hmac_key=km.hmac_key,
    )
    return km, se, record


def test_wrong_key_signature_rejected():
    from nacl.signing import SigningKey

    km, _, record = _one_signed_record(sign_seed=42)
    wrong = derive_keys(999)
    assert wrong.verify_key != km.verify_key  # precondition: distinct keys
    # Another device's key over the identical preimage: hash/HMAC check out,
    # only the Ed25519 verification must fail.
    desc = descriptor_bytes(
        record["counter"],
        record["window_start"],
        record["zc_samples"],
        record["window_flags"],
        record["device_id"],
        record["p_avg_q30"],
        record["energy_uwh"],
    )
    preimage = bytes.fromhex(record["hmac_hex"]) + desc + bytes.fromhex(record["window_hash_hex"])
    record["sig_hex"] = bytes(SigningKey(wrong.ed25519_seed).sign(preimage).signature).hex()
    rx = RecordReceiver(km.verify_key, km.hmac_key)
    verdict = rx.verify(record)
    assert not verdict.accepted and verdict.reason == "bad-signature"
    # And the wrong SE will not even sign a foreign HMAC (defense in depth).
    se_wrong = SecureElement.from_key_material(wrong)
    try:
        se_wrong.attest(
            desc,
            bytes.fromhex(record["window_hash_hex"]),
            bytes.fromhex(record["hmac_hex"]),
        )
        raise AssertionError("wrong SE signed a foreign HMAC")
    except Exception as exc:
        assert "HMAC mismatch" in str(exc)
