"""Week 7 attack campaign: run every in-scope attack 100+ times.

The blueprint's Week 7 runs each attack class 100+ times and produces a
detection table that includes the attacks we *miss* and why. The threat model
(``docs/threat_model.md``) fixes the in-scope classes:

    replay, spoofed V/I (with and without the secure element), bit flips,
    dropped packets, ledger edits/deletions, compromised MPC.

Every number in ``results/metrics.json['week7_attacks']`` is MEASURED from an
actual run of this module; nothing is fabricated. Each row records the
detection rate, the first verdicts observed, and the honest miss condition.

Detection surfaces (three, because the attacks live at three layers):

* L0 frame stream (``edge.receiver`` + ``edge.framing``): CRC16, monotonic
  counter (replay/reorder), counter gaps (drops). ``detected`` = the receiver
  rejects the attack (rejected-replay / CRC drop / reported gap).
* Ledger records (``ledger.verifier``): chain, HMAC/Ed25519 signature,
  descriptor self-consistency, counter continuity, gap rule. ``detected`` =
  the ledger is NOT accepted for issuance.
* Energy/savings (``plant.pump``): the compromised MPC is detected as NEGATIVE
  savings (the ledger stays valid — it is not a forgery). ``detected`` = the
  savings estimate drops below zero.

The keyless verifier holds only the public key (D-02), so a sample-level spoof
is NOT detectable by it: that is a documented miss, not a failure.

Run:  uv run python sim/attacks_week7.py
"""

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from edge.attestation import (  # noqa: E402
    SubWindow,
    build_window_hash,
    compute_hmac,
    descriptor_bytes,
)
from edge.framing import FrameDecoder, encode_frame  # noqa: E402
from edge.receiver import L0Receiver  # noqa: E402
from edge.secure_element import SecureElement  # noqa: E402
from ledger.verifier import LedgerVerifier  # noqa: E402
from plant.pump import (  # noqa: E402
    PumpParams,
    demand_profile,
    efficiency,
    grid_power_legacy,
    grid_power_retrofit,
    pump_head,
    solve_speed,
)
from sensors.windows import energy_uwh_increment, mean_q30_half_away  # noqa: E402
from sim.provision import derive_keys  # noqa: E402
from tests.week5d3_support import signed_chain  # noqa: E402

METRICS_PATH = REPO / "results" / "metrics.json"

N_TRIALS = 128  # >100 per attack, per blueprint
BASE_RECORDS = 5
L0_FRAMES = 10

# Compromised MPC: commands a fixed over-speed instead of the solved speed.
# Declared (not tuned to the outcome); the resulting savings are reported.
WASTE_OVER_SPEED = 1.1

ATTACKS = (
    "replay",
    "spoof_vi_with_se",
    "spoof_vi_without_se",
    "bitflip",
    "drop",
    "ledger_edit_delete",
    "compromised_mpc",
)


# --------------------------------------------------------------------------
# L0 frame-stream helpers
# --------------------------------------------------------------------------
def _l0_stream(seed: int, n: int = L0_FRAMES) -> list[bytes]:
    """n L0 frames with deterministic pseudo-random V/I."""
    rng = np.random.default_rng(seed)
    v = rng.integers(-32768, 32768, size=n).astype(np.int64)
    i = rng.integers(-32768, 32768, size=n).astype(np.int64)
    return [encode_frame(k, int(v[k]), int(i[k])) for k in range(n)]


def _ingest_all(frames: list[bytes]) -> L0Receiver:
    dec, rx = FrameDecoder(), L0Receiver()
    for raw in frames:
        for frame in dec.feed(raw):
            rx.ingest(frame)
    return rx


# --------------------------------------------------------------------------
# Record helpers
# --------------------------------------------------------------------------
def _rebuild_record(rec: dict, subs: list[SubWindow], km, se, prev_hash: bytes, sign: bool) -> dict:
    """Rebuild a record from (possibly spoofed) sub-windows.

    ``sign=False`` models an attacker WITHOUT the keys: the descriptor and
    window hash are recomputed over the spoofed samples, but the ORIGINAL
    hmac/signature are kept, so the verifier must reject (bad-signature).
    ``sign=True`` models the no-SE ablation where the attacker IS the signer:
    hmac and signature are recomputed with the keys and the record verifies.
    """
    from edge.attestation import N_SUBWINDOWS

    if len(subs) != N_SUBWINDOWS:
        raise ValueError("need 5 sub-windows")
    d = dict(rec)
    zc = [int(np.asarray(s.v_q15).shape[0]) if s.valid else 0 for s in subs]
    flags = 0
    for k, s in enumerate(subs):
        flags |= (1 if s.valid else 0) << k
    p_sum, p_count, energy = 0, 0, int(rec["energy_uwh"])
    for s, m in zip(subs, zc):
        if not s.valid:
            continue
        v = np.asarray(s.v_q15, dtype=np.int64)
        i = np.asarray(s.i_q15, dtype=np.int64)
        p = v * i
        p_sum += int(p.sum())
        p_count += int(p.shape[0])
        energy += energy_uwh_increment(int(p.sum()), m)
    d["zc_samples"] = zc
    d["window_flags"] = flags
    d["p_avg_q30"] = mean_q30_half_away(p_sum, p_count) if p_count else 0
    d["energy_uwh"] = max(energy, 0)
    desc = descriptor_bytes(
        int(d["counter"]),
        int(d["window_start"]),
        zc,
        flags,
        int(d["device_id"]),
        int(d["p_avg_q30"]),
        int(d["energy_uwh"]),
        int(d.get("overrun_cnt", 0)),
    )
    wh = build_window_hash(prev_hash, desc, subs)
    d["descriptor_hex"] = desc.hex()
    d["window_hash_hex"] = wh.hex()
    if sign:
        hmac_tag = compute_hmac(km.hmac_key, desc, wh)
        d["hmac_hex"] = hmac_tag.hex()
        d["sig_hex"] = se.attest(desc, wh, hmac_tag).hex()
    return d


def _subs_of(rec: dict) -> list[SubWindow]:
    return [
        SubWindow(
            v_q15=np.asarray(rec["samples_v"][k], dtype=np.int64),
            i_q15=np.asarray(rec["samples_i"][k], dtype=np.int64),
            valid=bool((int(rec["window_flags"]) >> k) & 1),
        )
        for k in range(len(rec["samples_v"]))
    ]


# --------------------------------------------------------------------------
# Attack applications (each returns the attacked ledger / stream)
# --------------------------------------------------------------------------
def _attack_replay(base: list[dict], seed: int) -> dict:
    """Replay a valid L0 frame AND a chain-patched duplicate ledger record."""
    stream = _l0_stream(seed)
    rx = _ingest_all(stream)
    replayed = list(FrameDecoder().feed(stream[3]))
    l0_verdict = rx.ingest(replayed[0]) if replayed else None
    alias = dict(base[1])
    alias["prev_hash_hex"] = base[-1]["window_hash_hex"]
    attacked = base + [alias]
    return {"stream": stream, "l0_verdict": l0_verdict, "records": attacked}


def _attack_spoof(base: list[dict], idx: int, seed: int, sign: bool) -> dict:
    """Spoof the V/I samples of ``base[idx]`` (default: last, no chain effect)."""
    km = derive_keys(seed)
    se = SecureElement.from_seed(seed)
    rec = base[idx]
    subs = _subs_of(rec)
    rng = np.random.default_rng(seed + 7)
    subs = [
        SubWindow(
            v_q15=s.v_q15 + int(rng.integers(-4000, 4000)),
            i_q15=s.i_q15 + int(rng.integers(-3000, 3000)),
            valid=s.valid,
        )
        for s in subs
    ]
    prev_hash = bytes.fromhex(rec["prev_hash_hex"])
    rebuilt = _rebuild_record(rec, subs, km, se, prev_hash, sign=sign)
    attacked = [dict(r) for r in base]
    attacked[idx] = rebuilt
    return {"records": attacked}


def _attack_bitflip(base: list[dict], seed: int) -> dict:
    """Flip one transport bit (L0 frame) and one descriptor bit (record)."""
    stream = _l0_stream(seed)
    bad = bytearray(stream[4])
    bad[2] ^= 0x40  # a payload bit (counter field)
    l0 = list(stream)
    l0[4] = bytes(bad)
    rx = _ingest_all(l0)
    records = [dict(r) for r in base]
    records[-1]["energy_uwh"] ^= 1  # one bit in a descriptor field, no re-sign
    return {"stream": l0, "l0_missing": list(rx.missing()), "records": records}


def _attack_drop(base: list[dict], seed: int) -> dict:
    """Drop one L0 frame and one ledger record."""
    stream = _l0_stream(seed)
    kept = [f for k, f in enumerate(stream) if k != 5]
    rx = _ingest_all(kept)
    records = [r for k, r in enumerate(base) if k != 2]
    return {"stream": kept, "l0_missing": list(rx.missing()), "records": records}


def _attack_ledger_edit_delete(base: list[dict], seed: int) -> dict:
    """Semantic edit of one field (no re-sign) and one deletion."""
    records = [dict(r) for r in base]
    records[1]["energy_uwh"] += 1  # edit, no re-sign -> bad-descriptor
    records = records[:2] + records[3:]  # delete record 2 -> bad-chain downstream
    return {"records": records}


def _attack_compromised_mpc(seed: int) -> dict:
    """Honest MPC vs compromised (over-speed) MPC savings on the pump plant."""
    p = PumpParams()
    fs = 10_000.0
    dt = 1.0 / fs
    t = np.arange(0.0, 60.0, dt)
    q = demand_profile(t, p)

    def energy(power: np.ndarray) -> float:
        return float(np.trapezoid(power, dx=dt))

    e_leg = energy(grid_power_legacy(q, p))
    n_hon = solve_speed(q, p)
    # Honest retrofit energy (VSD at the solved speed).
    e_hon = energy(grid_power_retrofit(q, p))
    # Compromised: command a fixed over-speed; the pump makes excess head.
    n_waste = WASTE_OVER_SPEED * n_hon
    h_waste = pump_head(q, n_waste, p)
    eta_waste = efficiency(q, n_waste, p)
    p_waste = p.rho_kgm3 * p.g_ms2 * q * h_waste / np.where(eta_waste > 0, eta_waste, np.nan)
    e_waste = energy(p_waste / p.eta_drive)
    return {
        "honest_savings_frac": (e_leg - e_hon) / e_leg,
        "compromised_savings_frac": (e_leg - e_waste) / e_leg,
        "e_legacy_kwh": e_leg / 3.6e6,
        "e_honest_kwh": e_hon / 3.6e6,
        "e_compromised_kwh": e_waste / 3.6e6,
    }


def run_attack(name: str, base: list[dict], seed: int, km, se) -> dict:
    """Apply one attack and return its measured detection result."""
    if name == "replay":
        out = _attack_replay(base, seed)
        verdict = LedgerVerifier(km.verify_key).verify(out["records"])
        return {
            "detected": (out["l0_verdict"] == "rejected-replay")
            and (not verdict.accepted_for_issuance),
            "l0_verdict": out["l0_verdict"],
            "verdicts": _first_verdicts(verdict),
        }
    if name in ("spoof_vi_with_se", "spoof_vi_without_se"):
        out = _attack_spoof(base, len(base) - 1, seed, sign=(name == "spoof_vi_without_se"))
        verdict = LedgerVerifier(km.verify_key).verify(out["records"])
        return {
            "detected": not verdict.accepted_for_issuance,
            "verdicts": _first_verdicts(verdict),
        }
    if name == "bitflip":
        out = _attack_bitflip(base, seed)
        verdict = LedgerVerifier(km.verify_key).verify(out["records"])
        return {
            "detected": (len(out["l0_missing"]) > 0) and (not verdict.accepted_for_issuance),
            "l0_missing": out["l0_missing"],
            "verdicts": _first_verdicts(verdict),
        }
    if name == "drop":
        out = _attack_drop(base, seed)
        verdict = LedgerVerifier(km.verify_key).verify(out["records"])
        return {
            "detected": (len(out["l0_missing"]) > 0) and (not verdict.accepted_for_issuance),
            "l0_missing": out["l0_missing"],
            "verdicts": _first_verdicts(verdict),
        }
    if name == "ledger_edit_delete":
        out = _attack_ledger_edit_delete(base, seed)
        verdict = LedgerVerifier(km.verify_key).verify(out["records"])
        return {"detected": not verdict.accepted_for_issuance, "verdicts": _first_verdicts(verdict)}
    if name == "compromised_mpc":
        out = _attack_compromised_mpc(seed)
        return {
            "detected": out["compromised_savings_frac"] < 0.0,
            "honest_savings_frac": out["honest_savings_frac"],
            "compromised_savings_frac": out["compromised_savings_frac"],
        }
    raise ValueError(f"unknown attack {name!r}")


def _first_verdicts(verdict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in verdict.outcomes:
        if not o.verified:
            counts[o.first_verdict] = counts.get(o.first_verdict, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Campaign
# --------------------------------------------------------------------------
def run_campaign(n_trials: int = N_TRIALS, seed: int = 42) -> dict:
    """Run every in-scope attack ``n_trials`` times with fresh seeds."""
    tally = {
        name: {"detected": 0, "missed": 0, "rate": None, "verdicts": {}, "notes": set()}
        for name in ATTACKS
    }
    clean_rejects = 0
    for t in range(n_trials):
        trial_seed = seed * 1000 + t
        base = signed_chain(BASE_RECORDS, seed=trial_seed)
        km = derive_keys(trial_seed)
        se = SecureElement.from_seed(trial_seed)

        # Clean control: an honest ledger must never be rejected (false-reject).
        if not LedgerVerifier(km.verify_key).verify(base).accepted_for_issuance:
            clean_rejects += 1

        for name in ATTACKS:
            res = run_attack(name, base, trial_seed, km, se)
            if res["detected"]:
                tally[name]["detected"] += 1
            else:
                tally[name]["missed"] += 1
            for verdict, count in res.get("verdicts", {}).items():
                tally[name]["verdicts"][verdict] = tally[name]["verdicts"].get(verdict, 0) + count
            for key in ("honest_savings_frac", "compromised_savings_frac"):
                if key in res:
                    tally[name]["notes"].add(f"{key}={res[key]:.6f}")

    detections = {}
    for name in ATTACKS:
        row = tally[name]
        row["rate"] = row["detected"] / n_trials
        row["verdicts"] = dict(sorted(row["verdicts"].items()))
        row["notes"] = sorted(row["notes"])
        detections[name] = row

    return {
        "label": "Week 7 attack campaign (N=128 trials/attack, fresh seeds)",
        "source": "sim/attacks_week7.py:run_campaign()",
        "seed": seed,
        "n_trials": n_trials,
        "base_records": BASE_RECORDS,
        "attacks": list(ATTACKS),
        "detections": detections,
        "false_reject_rate": clean_rejects / n_trials,
        "clean_trials": n_trials,
        "misses": [
            {
                "attack": "spoof_vi_without_se",
                "why": (
                    "Ablation by design: with no secure element in the path the "
                    "attacker is the signer, so the spoofed record re-signs and "
                    "verifies. This demonstrates why the SE is required."
                ),
            },
            {
                "attack": "sensor_spoof_pre_afe",
                "why": (
                    "Documented miss (threat model): a sample-level spoof at the "
                    "sensor is physics, not crypto. The keyless verifier holds "
                    "only the public key and has no samples, so it cannot "
                    "recompute the window hash; detection needs a physical "
                    "cross-check or the samples (capability matrix)."
                ),
            },
            {
                "attack": "compromised_mpc_waste",
                "why": (
                    "The physical waste itself is not prevented: the ledger stays "
                    "valid (no forgery), and the attack is detected only as "
                    "negative savings at the energy/baseline layer."
                ),
            },
        ],
    }


def save_week7_metrics(payload: dict) -> None:
    """Persist under ``results/metrics.json['week7_attacks']`` (strict JSON)."""
    existing = {}
    if METRICS_PATH.exists():
        existing = json.loads(METRICS_PATH.read_text())
    existing["week7_attacks"] = payload
    existing.setdefault("run_labels", {})["week7_attacks"] = {
        "label": payload["label"],
        "source": payload["source"],
        "seed": payload["seed"],
        "n_trials": payload["n_trials"],
    }
    json.dumps(existing, allow_nan=False)  # strict gate: raises on NaN/Infinity
    METRICS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"\nSaved week7_attacks metrics to {METRICS_PATH}")


def main() -> None:
    payload = run_campaign()
    save_week7_metrics(payload)
    print(
        f"trials/attack: {payload['n_trials']}; false-reject rate: "
        f"{payload['false_reject_rate']:.4f}"
    )
    for name in ATTACKS:
        row = payload["detections"][name]
        print(
            f"  {name:22s} detected {row['detected']:3d}/{payload['n_trials']} "
            f"rate {row['rate']:.3f} verdicts {row['verdicts']}"
        )


if __name__ == "__main__":
    main()
