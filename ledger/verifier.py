"""Keyless, sampleless ledger verifier (Week 5d3).

Trust boundary (D-02): the verifier holds ONLY the Ed25519 public key
(and optionally a checkpoint anchor). It never sees samples, the HMAC key,
or any secret material. Every integrity binding the FPGA published is
covered, transitively, by the Ed25519 signature ``SIG = Ed25519(HMAC ||
descriptor || window_hash)``:

- descriptor bytes are covered directly (CHV), the window hash is covered
  directly, the HMAC is covered *because it is inside the signing
  preimage* — the stored HMAC is bound by the signature even though this
  verifier cannot recompute it (capability matrix, Amendment 2).

Per-record checks run in a fixed canonical order; the FIRST failing check
is the record's headline verdict AND every failing check is collected
(Amendment 1), so a row can report e.g. ``bad-chain`` first with
``bad-replay`` also present. ``classify()`` maps a verdict to the Week 6
baseline class and is importable from a clean interpreter.

Canonical order (documented in DECISIONS.md):
malformed, bad-device-id, bad-descriptor, bad-overrun, bad-overrun-range,
bad-bit7, bad-genesis, bad-chain, bad-replay, bad-continuity, bad-gap,
bad-invalid-count, bad-neg-clamp, bad-range, bad-signature.

Ledger-level results: a reset event (second genesis) makes the ledger NOT
acceptable for issuance while per-record classification continues; an
anchor head that runs AHEAD of the ledger tail flags ``tail-truncation``;
records that overshoot the anchor flag ``anchor-mismatch``; with no anchor
the tail is ``unverified`` (documented miss — only the anchor bounds the
end).
"""

from dataclasses import dataclass, field
from typing import Optional

from edge.attestation import (
    GENESIS_HASH32,
    N_SUBWINDOWS,
    NEG_ENERGY_BIT,
    OVERRUN_BIT,
    descriptor_bytes,
)
from edge.secure_element import verify_signature

LEDGER_SCHEMA = "hrot-ledger-v1"
HASH_OVERFLOW_BIT = 7  # [bit] record aborted (ZQ overflow flag, RTL 0x80)
M_MIN = 1810  # [samples] golden window span floor (rtl/zc_detect.v)
M_MAX = 2230  # [samples] golden window span ceiling (rtl/zc_detect.v)
GAP_MIN_DROP_SPAN = 11  # [samples] minimum span of a dropped window (DECISIONS)

# Canonical order: index of a verdict decides "first failing check".
_ORDER = (
    "malformed",
    "bad-device-id",
    "bad-descriptor",
    "bad-overrun",
    "bad-overrun-range",
    "bad-bit7",
    "bad-genesis",  # before bad-chain: a second genesis reads as a reset event
    "bad-chain",
    "bad-replay",
    "bad-continuity",
    "bad-gap",
    "bad-invalid-count",
    "bad-neg-clamp",
    "bad-range",
    "bad-signature",
)


@dataclass
class Anchor:
    """Trusted checkpoint supplied OUT-of-band (public anchor material)."""

    expected_last_counter: int
    head_hash: Optional[bytes] = None  # verified against the head record's window hash


@dataclass
class RecordOutcome:
    """Per-record verdict: headline (first failure) + every failure."""

    index: int
    counter: int
    first_verdict: str
    failures: tuple[str, ...]
    verified: bool
    classification: str


@dataclass
class LedgerVerdict:
    """Whole-ledger result: per-record outcomes, class counts, tail state."""

    outcomes: list[RecordOutcome]
    counts: dict[str, int] = field(default_factory=dict)
    reset_events: int = 0  # second genesis observed (policy-rejected ledger)
    tail_truncated: bool = False
    anchor_mismatch: bool = False
    unverified_tail: bool = False  # no anchor supplied
    accepted_for_issuance: bool = True


def classify(record: dict, failures=()) -> str:
    """Map a verified record to the Week 6 baseline class (importable).

    ``rejected``: any check failed, or bit 7 set (Q1: bit 7 is REJECTED,
    never merely excluded). ``excluded``: verified but energy NOT counted
    — bit 5 NEG clamp, or an all-invalid record (no valid sub-window, so
    p_avg 0 and zero energy contribution). ``valid``: verified and energy
    attributable. Counts are summed by :func:`LedgerVerifier.verify`.
    """
    failures = tuple(failures)
    if failures or (int(record.get("window_flags", 0)) >> HASH_OVERFLOW_BIT) & 1:
        return "rejected"
    flags = int(record.get("window_flags", 0))
    neg_clamped = (flags >> NEG_ENERGY_BIT) & 1
    all_invalid = not (flags & ((1 << N_SUBWINDOWS) - 1))
    if neg_clamped or all_invalid:
        return "excluded"
    return "valid"


def _overrun_flag(rec: dict) -> int:
    return (int(rec["window_flags"]) >> OVERRUN_BIT) & 1


def _descriptor(rec: dict) -> bytes:
    return descriptor_bytes(
        int(rec["counter"]),
        int(rec["window_start"]),
        [int(m) for m in rec["zc_samples"]],
        int(rec["window_flags"]),
        int(rec["device_id"]),
        int(rec["p_avg_q30"]),
        int(rec["energy_uwh"]),
        int(rec.get("overrun_cnt", 0)),
    )


class LedgerVerifier:
    """Verify a ledger with only the public key (plus an optional anchor)."""

    def __init__(
        self,
        verify_key: bytes,
        expected_device_id: int = 1,
        anchor: Optional[Anchor] = None,
        schema_req: bool = True,
        check_zc_range: bool = True,
    ):
        self.verify_key = bytes(verify_key)
        self.expected_device_id = int(expected_device_id)
        self.anchor = anchor
        self.schema_req = schema_req
        self.check_zc_range = check_zc_range

    def verify(self, records: list[dict]) -> LedgerVerdict:  # noqa: C901
        """Run every per-record check over the ledger lines, then tail checks."""
        outcomes: list[RecordOutcome] = []
        prev_hash = GENESIS_HASH32
        seen: set[int] = set()
        prev_timeline: Optional[tuple[int, int]] = None  # (win_start, attested span)
        prev_parseable = False
        reset_events = 0

        for idx, rec in enumerate(records):
            failures = []
            counter = None
            try:
                counter = self._check_preamble(rec, failures, idx)
            except _Malformed:
                outcomes.append(
                    RecordOutcome(idx, -1, "malformed", ("malformed",), False, "rejected")
                )
                prev_timeline = None
                prev_parseable = False
                continue
            # ---------------- chain-level checks ----------------
            if idx > 0:
                prev_link = bytes.fromhex(rec["prev_hash_hex"])
                if prev_link != prev_hash:
                    failures.append("bad-chain")
                if counter == 0:  # second genesis: policy-rejected reset event
                    failures.append("bad-genesis")
                    reset_events += 1
            elif counter != 0 or rec["prev_hash_hex"] != GENESIS_HASH32.hex():
                failures.append("bad-genesis")  # head truncation / forged genesis
            if counter in seen:
                failures.append("bad-replay")
            seen.add(counter)
            if idx > 0 and prev_parseable:
                prev_counter = outcomes[idx - 1].counter
                if counter != prev_counter + 1 and counter != 0:
                    failures.append("bad-continuity")
            # ---------------- gap / timeline rule ----------------
            if prev_parseable and prev_timeline is not None:
                failures.extend(self._check_gap(idx, records, prev_timeline))
            prev_timeline = (
                int(rec["window_start"]),
                sum(int(m) for m in rec["zc_samples"]),
            )
            prev_parseable = True
            # ---------------- field-level semantic checks ----------------
            flags = int(rec["window_flags"])
            if not (flags & (1 << HASH_OVERFLOW_BIT)):  # bit 7 abandons the record
                for k in range(N_SUBWINDOWS):
                    if not (flags >> k) & 1 and int(rec["zc_samples"][k]) != 0:
                        failures.append("bad-invalid-count")
                        break
                if (flags >> NEG_ENERGY_BIT) & 1 and int(rec["energy_uwh"]) != 0:
                    failures.append("bad-neg-clamp")
                for k in range(N_SUBWINDOWS):
                    m = int(rec["zc_samples"][k])
                    if self.check_zc_range and (flags >> k) & 1 and not (M_MIN <= m <= M_MAX):
                        failures.append("bad-range")
                        break
            # ---------------- signature (binds HMAC || desc || hash) ----------------
            if not self._sig_ok(rec):
                failures.append("bad-signature")
            order = [_ORDER.index(f) for f in failures]
            verified = not failures
            outcomes.append(
                RecordOutcome(
                    idx,
                    counter,
                    "ok" if verified else _ORDER[min(order)],
                    tuple(sorted(failures, key=_ORDER.index)),
                    verified,
                    classify(rec, failures),
                )
            )
            prev_hash = bytes.fromhex(rec["window_hash_hex"])

        counts = {"valid": 0, "excluded": 0, "rejected": 0}
        for o in outcomes:
            counts[o.classification] += 1

        verdict = LedgerVerdict(
            outcomes=outcomes,
            counts=counts,
            reset_events=reset_events,
            accepted_for_issuance=not any(o.verified is False for o in outcomes),
        )
        if reset_events > 0:
            verdict.accepted_for_issuance = False

        head = outcomes[-1] if outcomes else None
        if self.anchor is not None:
            if head is None or head.counter < self.anchor.expected_last_counter:
                verdict.tail_truncated = True
                verdict.accepted_for_issuance = False
            elif head.counter > self.anchor.expected_last_counter:
                verdict.anchor_mismatch = True
                verdict.accepted_for_issuance = False
            else:
                head_rec = records[head.index]
                got_hash = bytes.fromhex(head_rec["window_hash_hex"])
                if self.anchor.head_hash is not None and got_hash != self.anchor.head_hash:
                    verdict.anchor_mismatch = True
                    verdict.accepted_for_issuance = False
        else:
            verdict.unverified_tail = True
        return verdict

    def _check_preamble(self, rec: dict, failures: list[str], idx: int) -> int:
        """Structural + field-level checks; return the counter (or raise)."""
        if self.schema_req and rec.get("schema") != LEDGER_SCHEMA:
            raise _Malformed("schema")
        try:
            counter = int(rec["counter"])
            _ = int(rec["window_start"])  # parse now; consumed by the gap rule
            dev = int(rec["device_id"])
            if dev != self.expected_device_id:
                failures.append("bad-device-id")
            zc = [int(m) for m in rec["zc_samples"]]
            if len(zc) != N_SUBWINDOWS:
                raise _Malformed("zc_samples")
            flags = int(rec["window_flags"])
            overrun = int(rec.get("overrun_cnt", 0))
            if (_overrun_flag(rec)) != (1 if overrun > 0 else 0):
                failures.append("bad-overrun")
            if overrun > 5:
                failures.append("bad-overrun-range")
            if (flags >> HASH_OVERFLOW_BIT) & 1:
                failures.append("bad-bit7")
            payload = _descriptor(rec)
            if payload.hex() != rec["descriptor_hex"]:
                failures.append("bad-descriptor")
            return counter
        except (KeyError, ValueError, TypeError) as exc:
            raise _Malformed(str(exc)) from exc

    def _check_gap(  # noqa: E501
        self, idx: int, records: list[dict], prev_timeline: tuple[int, int]
    ) -> list[str]:
        """Gap rule (DECISIONS): gap==0 with bit6 clear; >= count*11 with bit6 set."""
        rec, prev = records[idx], records[idx - 1]
        prev_win_start, prev_attested = prev_timeline
        gap = int(rec["window_start"]) - prev_win_start - prev_attested
        if _overrun_flag(prev):
            low = int(prev.get("overrun_cnt", 0)) * GAP_MIN_DROP_SPAN
            return [] if gap >= low else ["bad-gap"]
        return [] if gap == 0 else ["bad-gap"]

    def _sig_ok(self, rec: dict) -> bool:
        try:
            hmac_tag = bytes.fromhex(rec["hmac_hex"])
            desc = bytes.fromhex(rec["descriptor_hex"])
            w_hash = bytes.fromhex(rec["window_hash_hex"])
            sig = bytes.fromhex(rec["sig_hex"])
        except (ValueError, KeyError):
            return False
        if len(hmac_tag) != 32 or len(desc) != 36 or len(w_hash) != 32 or len(sig) != 64:
            return False
        return verify_signature(
            self.verify_key, sig, hmac_tag + desc + w_hash
        )  # preimage layout per SecureElement


class _Malformed(Exception):
    """Structural parse failure: the line is not a usable ledger record."""


def main(argv=None) -> int:
    """CLI: python -m ledger.verifier <ledger> <verify_key_hex> [options]."""
    import argparse

    from ledger.writer import read_ledger

    parser = argparse.ArgumentParser(description="Keyless/sampleless ledger verification.")
    parser.add_argument("ledger", type=str, help="path to ledger JSONL")
    parser.add_argument("verify_key_hex", type=str, help="Ed25519 public key (hex)")
    parser.add_argument("--device", type=int, default=1, help="expected device_id")
    parser.add_argument("--anchor-counter", type=int, default=None, help="trusted last counter")
    args = parser.parse_args(argv)
    anchor = Anchor(args.anchor_counter) if args.anchor_counter is not None else None
    verifier = LedgerVerifier(
        bytes.fromhex(args.verify_key_hex), expected_device_id=args.device, anchor=anchor
    )
    verdict = verifier.verify(read_ledger(args.ledger))
    print(
        f"records={len(verdict.outcomes)} valid={verdict.counts['valid']} "
        f"excluded={verdict.counts['excluded']} rejected={verdict.counts['rejected']} "
        f"reset={verdict.reset_events} tail_truncated={verdict.tail_truncated} "
        f"anchor_mismatch={verdict.anchor_mismatch} unverified_tail={verdict.unverified_tail} "
        f"accepted_for_issuance={verdict.accepted_for_issuance}"
    )
    for outcome in verdict.outcomes:
        if not outcome.verified:
            label = (
                f"  rec[{outcome.index}] counter={outcome.counter} "
                f"first={outcome.first_verdict} all={','.join(outcome.failures)}"
            )
            print(label)
    return 0 if verdict.accepted_for_issuance else 1
