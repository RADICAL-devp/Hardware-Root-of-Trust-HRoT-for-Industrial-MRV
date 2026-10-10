"""Shared builders for Week 5d3 writer/verifier tests (synthetic, unit-level).

These helpers build *signed, chain-consistent* golden ledgers with the
Python model (`edge.attestation.build_record`) plus `SecureElement.attest`,
so every row is internally valid material — the tamper/mutation tests then
break exactly ONE property and assert the verifier names it first.

Honesty notes (see DECISIONS.md):
- The golden timeline advances the declared ``window_start`` by the
  *attested* span (sum of ``zc_samples``) plus an overrun record's
  ``overrun_cnt * 11`` drop allowance (GAP_MIN_DROP_SPAN boundary). This
  mirrors what the strict gap rule accepts; an RTL natural-invalid window
  that advances the physical timeline without bit 6 is NOT representable
  here by construction (over-rejection limitation, capability matrix).
- Re-minted rows are white-box unit material: an attacker without the SE
  key cannot re-sign. They isolate one semantic rule so the first verdict
  is the interesting one rather than bad-signature.
"""

import numpy as np

from edge.attestation import (
    GENESIS_HASH32,
    N_SUBWINDOWS,
    SubWindow,
    build_record,
    compute_hmac,
    descriptor_bytes,
)
from edge.secure_element import SecureElement
from ledger.verifier import LEDGER_SCHEMA
from sim.provision import derive_keys

HASH_OVERFLOW_BIT = 7  # [bit] record aborted (must mirror rtl AND verifier)


def make_subs(m: int, seed: int, valid_flags: int = 0x1F, invert: bool = False) -> list[SubWindow]:
    """Five deterministic sine sub-windows of m samples; valid bits per flags."""
    rng = np.random.default_rng(seed)
    t = np.arange(m)
    lam = 200.0
    subs = []
    for k in range(N_SUBWINDOWS):
        valid = bool(valid_flags & (1 << k))
        phase = rng.uniform(0.0, 2.0 * np.pi)
        v = (np.sin(2.0 * np.pi * t / lam + phase) * 20000).round().astype(np.int64)
        i = (np.sin(2.0 * np.pi * t / lam + phase + 1.3) * 12000).round().astype(np.int64)
        if invert:  # reactive i flips sign: negative per-sample power
            i = -i
        subs.append(SubWindow(v_q15=v, i_q15=i, valid=valid))
    return subs


def signed_chain(
    n: int,
    seed: int = 42,
    device_id: int = 1,
    wstart0: int = 100_000,
    window_lens=2000,
    flags=None,
    overruns=None,
    neg=None,
) -> list[dict]:
    """n chained, signed records; window_lens/flags/overruns/neg: scalar or per-idx list.

    Timeline advance per record = attested span + overrun_cnt * 11, so every
    gap lands exactly on the boundary the bit-6 rule accepts (see module
    docstring). Each record carries ``schema`` for direct verifier input.
    """
    if not callable(flags) and not isinstance(flags, list):
        rec_flags = [0x1F if flags is None else flags] * n
    elif isinstance(flags, list):
        rec_flags = list(flags)
    else:  # callable
        rec_flags = [flags(i) for i in range(n)]

    def _pick(v, i):
        if v is None:
            return 0
        if callable(v):
            return v(i)
        return v[i] if isinstance(v, list) else v

    km = derive_keys(seed)
    se = SecureElement.from_seed(seed)
    records = []
    prev = GENESIS_HASH32
    wstart = int(wstart0)
    energy_prev = 0
    for idx in range(n):
        m = _pick(window_lens, idx)
        flag_bits = int(rec_flags[idx])
        invert = bool(_pick(neg, idx))
        ov = int(_pick(overruns, idx))
        subs = make_subs(int(m), seed + idx, valid_flags=flag_bits, invert=invert)
        rec, (desc, wh, hmac) = build_record(
            idx,
            wstart,
            subs,
            device_id,
            prev,
            energy_prev,
            km.hmac_key,
            overrun_cnt=ov,
        )
        sig = se.attest(desc, wh, hmac)
        rec["sig_hex"] = sig.hex()
        rec["schema"] = LEDGER_SCHEMA
        records.append(rec)
        attested = sum(int(z) for z in rec["zc_samples"])
        wstart += attested + int(ov) * 11
        prev = wh
        energy_prev = int(rec["energy_uwh"])  # clamped NEG carries as 0
    return records


def re_mint(rec: dict, seed: int = 42, **fields) -> dict:
    """Re-sign an existing record with new descriptor fields (white-box, unit-level).

    Keeps ``prev_hash_hex`` and ``window_hash_hex`` (chain position) and
    recomputes descriptor + HMAC + signature so ONLY the modified rule can
    fail. An attacker without the SE key cannot do this.
    """
    km = derive_keys(seed)
    se = SecureElement.from_seed(seed)
    d = dict(rec)
    for key, value in fields.items():
        if key == "zc_samples":
            d[key] = [int(m) for m in value]
        elif key in {
            "window_flags",
            "counter",
            "window_start",
            "p_avg_q30",
            "energy_uwh",
            "device_id",
            "overrun_cnt",
        }:
            d[key] = int(value)
        else:
            raise ValueError(f"re_mint cannot touch {key!r}")
    desc = descriptor_bytes(
        d["counter"],
        d["window_start"],
        [int(m) for m in d["zc_samples"]],
        d["window_flags"],
        d["device_id"],
        d["p_avg_q30"],
        d["energy_uwh"],
        d["overrun_cnt"],
    )
    wh = bytes.fromhex(d["window_hash_hex"])
    hmac_tag = compute_hmac(km.hmac_key, desc, wh)
    sig = se.attest(desc, wh, hmac_tag)
    d["descriptor_hex"] = desc.hex()
    d["hmac_hex"] = hmac_tag.hex()
    d["sig_hex"] = sig.hex()
    return d


def ledger_line(rec: dict) -> str:
    """Serialize a dict exactly like ``ledger.writer.write_ledger`` lines."""
    import json

    return json.dumps(rec)


def outcome_map(verdict) -> dict[int, "RecordOutcome"]:  # noqa: F821
    return {o.index: o for o in verdict.outcomes}
