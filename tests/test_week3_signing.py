"""Week 3: provisioning, SE key custody, D-05 record semantics at scale.

Covers seeded provisioning (no hardcoded keys), private-key custody,
invalid-flagged windows as countable records, post-signing bit-flips in
`window_flags`/`zc_samples`, zero false rejects over 10,000 seeded L0
frames end to end and over 10,000 seeded mixed windows.
"""

import numpy as np

from edge.attestation import GENESIS_HASH32, SubWindow, build_record
from edge.framing import FrameDecoder, encode_frame, q15_encode
from edge.receiver import L0Receiver, RecordReceiver
from edge.secure_element import SecureElement
from plant.load_profiles import make_profile
from plant.motor import MotorParams, simulate
from sensors import measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams
from sensors.windows import ZCParams, build_windows, find_rising_crossings
from sim.provision import derive_keys, provision_to_dir

SEED = 42
FS_HZ = 10_000.0


def test_provision_deterministic_and_seed_sensitive(tmp_path):
    a, b, c = derive_keys(42), derive_keys(42), derive_keys(43)
    assert a.ed25519_seed == b.ed25519_seed and a.hmac_key == b.hmac_key
    assert a.ed25519_seed != c.ed25519_seed and a.hmac_key != c.hmac_key
    assert a.device_id == 1 and len(a.verify_key) == 32
    out_a = provision_to_dir(42, tmp_path / "a")
    out_b = provision_to_dir(42, tmp_path / "b")
    for name in ("ed25519_seed.bin", "hmac_key.bin", "device.json"):
        assert out_a[name].read_bytes() == out_b[name].read_bytes()
    assert (tmp_path / "a" / "ed25519_seed.bin").read_bytes() != derive_keys(43).ed25519_seed


def test_se_private_key_never_exposed():
    from edge.attestation import compute_hmac, descriptor_bytes

    se = SecureElement.from_seed(SEED)
    public = {m for m in dir(se) if not m.startswith("_")}
    assert public <= {"attest", "public_key", "from_seed", "from_key_material"}
    for name in ("signing_key", "private_key", "seed", "secret", "ed25519_seed"):
        assert getattr(se, name, None) is None
    assert se.public_key == derive_keys(SEED).verify_key  # oracle-only use works
    km = derive_keys(SEED)
    desc = descriptor_bytes(0, 0, [0, 0, 0, 0, 0], 0, km.device_id, 0, 0)
    good_hmac = compute_hmac(km.hmac_key, desc, GENESIS_HASH32)
    assert len(se.attest(desc, GENESIS_HASH32, good_hmac)) == 64
    bad_hmac = bytearray(good_hmac)
    bad_hmac[0] ^= 1
    try:
        se.attest(desc, GENESIS_HASH32, bytes(bad_hmac))
        raise AssertionError("SE signed over a forged HMAC")
    except Exception as exc:
        assert "HMAC mismatch" in str(exc)
    # Refusal leaves SE state unchanged: the same instance still attests.
    assert len(se.attest(desc, GENESIS_HASH32, good_hmac)) == 64


def _sign_record(km, se, counter, subs, window_start, energy_prev):
    record, _ = build_record(
        counter=counter,
        window_start=window_start,
        sub_windows=subs,
        device_id=km.device_id,
        prev_hash=GENESIS_HASH32,
        energy_prev_uwh=energy_prev,
        hmac_key=km.hmac_key,
    )
    record["sig_hex"] = se.attest(
        bytes.fromhex(record["descriptor_hex"]),
        bytes.fromhex(record["window_hash_hex"]),
        bytes.fromhex(record["hmac_hex"]),
    ).hex()
    return record


def test_invalid_flagged_window_verifies_and_advances_counter():
    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    rx = RecordReceiver(km.verify_key, km.hmac_key)
    n = 500
    good = SubWindow(
        v_q15=np.full(n, 15000, dtype=np.int64),
        i_q15=np.full(n, 6000, dtype=np.int64),
        valid=True,
    )
    bad = SubWindow(
        v_q15=np.zeros(n, dtype=np.int64), i_q15=np.zeros(n, dtype=np.int64), valid=False
    )
    subs = [good, good, bad, good, bad]  # flags 0b01011, mixed valid/invalid
    records = [
        _sign_record(km, se, 7, subs, 70_000, 0),
        _sign_record(km, se, 8, [bad] * 5, 80_000, 0),  # all-invalid window
        _sign_record(km, se, 9, subs, 90_000, 0),
    ]
    assert records[0]["window_flags"] == 0b01011
    assert records[1]["window_flags"] == 0b00000
    for record in records:  # invalid-flagged records verify like any other ...
        verdict = rx.verify(record)
        assert verdict.accepted, verdict.reason
    assert rx.counters() == [7, 8, 9]  # ... and advance the counter, not a drop.


def test_bitflip_in_flags_or_zcsamples_after_signing_rejected():
    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    n = 300
    subs = [
        SubWindow(
            v_q15=np.full(n, 12000, dtype=np.int64),
            i_q15=np.full(n, 5000, dtype=np.int64),
            valid=bool(k % 2),
        )
        for k in range(5)
    ]
    record = _sign_record(km, se, 3, subs, 30_000, 0)
    assert RecordReceiver(km.verify_key, km.hmac_key).verify(record).accepted
    blob = bytearray(10 + 1)  # zc_samples (10 B) || window_flags (1 B)
    blob[0:10] = bytes(record["zc_samples_raw"])
    blob[10] = record["window_flags"]
    for bit in range(8 * len(blob)):  # every bit position, no sampling
        tampered = dict(record)
        mut = bytearray(blob)
        mut[bit // 8] ^= 1 << (bit % 8)
        tampered["zc_samples"] = [
            int.from_bytes(mut[2 * k : 2 * k + 2], "little") for k in range(5)
        ]
        tampered["window_flags"] = mut[10]
        # Fresh receiver per flip: a reused counter would short-circuit on
        # replay before reaching the cryptographic checks.
        rx = RecordReceiver(km.verify_key, km.hmac_key)
        verdict = rx.verify(tampered)
        assert not verdict.accepted, f"bit {bit} in flags/zc_samples slipped through"


def test_bitflip_counter_windowstart_reserved_rejected():
    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    n = 300
    subs = [
        SubWindow(
            v_q15=np.full(n, 12000, dtype=np.int64),
            i_q15=np.full(n, 5000, dtype=np.int64),
            valid=True,
        )
        for _ in range(5)
    ]
    record = _sign_record(km, se, 11, subs, 110_000, 0)
    assert RecordReceiver(km.verify_key, km.hmac_key).verify(record).accepted
    desc = bytearray.fromhex(record["descriptor_hex"])
    assert len(desc) == 36
    # Descriptor-relative offsets (preimage off 32): counter [0, 4),
    # window_start [4, 8), reserved byte 19.
    targets = list(range(0, 8)) + [19]
    for byte_off in targets:
        for bit in range(8):  # every bit position, no sampling
            tampered = dict(record)
            mut = bytearray(desc)
            mut[byte_off] ^= 1 << bit
            tampered["descriptor_hex"] = bytes(mut).hex()
            rx = RecordReceiver(km.verify_key, km.hmac_key)
            verdict = rx.verify(tampered)
            assert not verdict.accepted, f"byte {byte_off} bit {bit} slipped through"


def test_device_id_mismatch_rejected():
    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    n = 300
    subs = [
        SubWindow(
            v_q15=np.full(n, 12000, dtype=np.int64),
            i_q15=np.full(n, 5000, dtype=np.int64),
            valid=True,
        )
        for _ in range(5)
    ]
    record = _sign_record(km, se, 12, subs, 120_000, 0)
    assert RecordReceiver(km.verify_key, km.hmac_key).verify(record).accepted
    for bit in range(32):  # every device_id bit: a foreign device must not verify
        tampered = dict(record)
        tampered["device_id"] = record["device_id"] ^ (1 << bit)
        rx = RecordReceiver(km.verify_key, km.hmac_key)
        verdict = rx.verify(tampered)
        assert not verdict.accepted, f"device_id bit {bit} slipped through"


def test_negative_energy_clamped_with_flag_not_wrapped():
    from edge.attestation import NEG_ENERGY_BIT

    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    n = 2000
    subs = [  # regenerative sign: negative power on every valid sub-window
        SubWindow(
            v_q15=np.full(n, 12000, dtype=np.int64),
            i_q15=np.full(n, -5000, dtype=np.int64),
            valid=True,
        )
        for _ in range(5)
    ]
    record = _sign_record(km, se, 21, subs, 210_000, 0)
    assert record["p_avg_q30"] == -60_000_000  # P_AVG stays signed-exact
    assert record["energy_uwh"] == 0  # clamped, never wrapped or raised
    assert (record["window_flags"] >> NEG_ENERGY_BIT) & 1 == 1
    verdict = RecordReceiver(km.verify_key, km.hmac_key).verify(record)
    assert verdict.accepted, verdict.reason  # clamped records still verify


def test_mixed_10k_windows_zero_false_rejects():
    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    rx = RecordReceiver(km.verify_key, km.hmac_key)
    rng = np.random.default_rng(SEED)
    zc = ZCParams()
    energy, invalid_records, base = 0, 0, 0
    n_records = 2000  # 2000 records x 5 sub-windows = 10,000 seeded windows
    for counter in range(n_records):
        subs = []
        for _ in range(5):
            valid = bool(rng.random() < 0.9)  # seeded valid/invalid mix
            m = int(rng.integers(zc.m_min_samples, zc.m_max_samples + 1)) if valid else 0
            # Power-positive load-like codes (mean P >> 0, as on a real motor);
            # the crypto/counter path under test needs physical energy signs.
            v = 20000 + rng.integers(-2000, 2001, size=max(m, 1))
            i = 8000 + rng.integers(-1000, 1001, size=max(m, 1))
            subs.append(
                SubWindow(
                    v_q15=np.clip(v, -32768, 32767).astype(np.int64),
                    i_q15=np.clip(i, -32768, 32767).astype(np.int64),
                    valid=valid,
                )
            )
        record = _sign_record(km, se, counter, subs, base, energy)
        energy = record["energy_uwh"]
        base += 10_000
        verdict = rx.verify(record)
        assert verdict.accepted, (counter, verdict.reason)  # zero false rejects
        invalid_records += sum(1 for s in subs if not s.valid)
    assert rx.counters() == list(range(n_records))  # continuity holds throughout
    assert rx.missing() == []
    assert invalid_records > 500  # the mix really contained invalid windows
    print(f"\n10k windows: {n_records} records verified, {invalid_records} invalid sub-windows")


def test_real_detector_windows_feed_record_path():
    km = derive_keys(SEED)
    se = SecureElement.from_key_material(km)
    load = make_profile("steady", duration_s=5.0, fs_hz=FS_HZ, seed=SEED)
    res = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    v_q15 = np.array([q15_encode(x, 500.0) for x in v_m], dtype=np.int64)
    i_q15 = np.array([q15_encode(x, 100.0) for x in i_m], dtype=np.int64)
    det_idx, frac = find_rising_crossings(v_m, FS_HZ, ZCParams().hyst_V)
    windows = build_windows(det_idx, frac, v_m.size, ZCParams())
    first5 = windows[:5]
    assert all(w.valid for w in first5)
    subs = [
        SubWindow(
            v_q15=v_q15[w.start_idx : w.end_idx],
            i_q15=i_q15[w.start_idx : w.end_idx],
            valid=True,
        )
        for w in first5
    ]
    record = _sign_record(km, se, 0, subs, first5[0].start_idx, 0)
    assert record["zc_samples_raw"] is not None  # exact counts travel with the record
    verdict = RecordReceiver(km.verify_key, km.hmac_key).verify(record)
    assert verdict.accepted, verdict.reason


def test_e2e_10k_frames_zero_false_rejects():
    n = 10_000  # exactly 1.0 s of telemetry at 10 kHz
    load = make_profile("steady", duration_s=1.0, fs_hz=FS_HZ, seed=SEED)
    res = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    t = np.arange(n) / FS_HZ
    v_m, i_m = measurement_chain(
        res.v_true_V[:n], res.i_true_A[:n], t, AFE(AFEParams(seed=SEED)), ADCParams()
    )
    stream = b"".join(
        encode_frame(k, q15_encode(v_m[k], 500.0), q15_encode(i_m[k], 100.0)) for k in range(n)
    )
    dec, rx, codes = FrameDecoder(), L0Receiver(), []
    for off in range(0, len(stream), 997):  # uneven chunks incl. mid-frame splits
        for frame in dec.feed(stream[off : off + 997]):
            assert rx.ingest(frame) == "ok"
            codes.append((frame.counter, frame.v_q15, frame.i_q15))
    assert len(codes) == n  # zero false rejects over 10,000 seeded frames
    assert rx.missing() == [] and rx.gaps == [] and rx.rejected_reorders == []
    for k, v, i in codes:  # L0 transport is bit-exact on the codes
        assert (v, i) == (q15_encode(v_m[k], 500.0), q15_encode(i_m[k], 100.0))
    print(f"\ne2e: {len(codes)} frames accepted, gaps={rx.gaps}, rejected={rx.rejected_reorders}")
