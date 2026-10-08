"""Pure-Python reference receiver (Week 3 test harness, not the Week 5 verifier).

L0 stream policy (STRICT REJECT, decided post-Week-3-review, supersedes the
planning-phase accept-and-flag): `counter` values form a seen set with a
`max_seen` watermark. An already-seen counter is REJECTED as replay; an
unseen counter below the watermark is REJECTED as reordered — dropped, not
added to the set, counted in `rejected_reorders`, and surfacing as a gap
via `missing()`. A jump past `max_seen + 1` is accepted with a recorded
gap; end-of-stream counters in `[0, max_seen]` absent from the set are
drops — reported via `missing()`, never silent. Rationale: UART is
in-order, so disorder is attack-indicative; nothing is silently
reordered.

Record policy: the receiver rebuilds the descriptor bytes from the
record's integer fields AND cross-checks them against the transported
`descriptor_hex` (either direction of tampering — fields or encoding,
including the reserved byte — is rejected as `bad-descriptor`),
recomputes the window hash from the transported samples, checks the HMAC
in constant time, then the Ed25519 signature against the provisioned
public key. An invalid-flagged window verifies exactly like a valid one
and advances the window counter: flags are records, not drops.
"""

from dataclasses import dataclass, field

import numpy as np

from edge.attestation import (
    OVERRUN_BIT,
    SubWindow,
    build_window_hash,
    compute_hmac,
    descriptor_bytes,
)
from edge.framing import Frame
from edge.secure_element import verify_signature


@dataclass
class CounterTracker:
    """Monotonic-counter continuity tracker shared by both receiver levels."""

    seen: set[int] = field(default_factory=set)
    max_seen: int | None = None
    gaps: list[tuple[int, int]] = field(default_factory=list)
    rejected_reorders: list[int] = field(default_factory=list)

    def ingest(self, counter: int) -> str:
        """Ingest a counter; return ok | ok-gap | rejected-replay | rejected-reorder."""
        counter = int(counter)
        if counter in self.seen:
            return "rejected-replay"
        if self.max_seen is None or counter == self.max_seen + 1:
            self.seen.add(counter)
            self.max_seen = counter
            return "ok"
        if counter > self.max_seen + 1:
            self.seen.add(counter)
            self.gaps.append((self.max_seen + 1, counter - 1))
            self.max_seen = counter
            return "ok-gap"
        # Unseen counter below the watermark: strict reject. Dropped (never
        # added to seen), counted, and surfacing as a gap via missing().
        self.rejected_reorders.append(counter)
        return "rejected-reorder"

    def missing(self) -> list[int]:
        """Counters in [0, max_seen] never seen: detected drops."""
        if self.max_seen is None:
            return []
        return [c for c in range(self.max_seen + 1) if c not in self.seen]


@dataclass
class L0Receiver:
    """L0 stream receiver: CRC-clean frames in, continuity verdicts out."""

    tracker: CounterTracker = field(default_factory=CounterTracker)

    def ingest(self, frame: Frame) -> str:
        """Ingest a decoded frame; return the continuity verdict."""
        return self.tracker.ingest(frame.counter)

    @property
    def seen(self) -> set[int]:
        """[counts] accepted L0 counters."""
        return self.tracker.seen

    @property
    def gaps(self) -> list[tuple[int, int]]:
        """[(lo, hi)] accepted jumps over unseen counters."""
        return self.tracker.gaps

    @property
    def rejected_reorders(self) -> list[int]:
        """[counts] late unseen counters dropped under strict reject."""
        return self.tracker.rejected_reorders

    def missing(self) -> list[int]:
        """[counts] detected drops (see `CounterTracker.missing`)."""
        return self.tracker.missing()


@dataclass
class RecordVerdict:
    """Outcome of one attestation-record verification."""

    accepted: bool  # [flag] True iff hash + HMAC + signature (+ continuity) hold
    reason: str  # [text] ok | ok-gap | rejected-* | malformed | bad-* | bad-descriptor
    counter: int | None = None  # [counts] window counter under verification


@dataclass
class RecordReceiver:
    """Window-record verifier holding the provisioned public + HMAC keys."""

    verify_key: bytes  # [bytes] 32 B Ed25519 public key (only key the verifier needs)
    hmac_key: bytes  # [bytes] 32 B HMAC key (test harness only; Week 5 drops this)
    tracker: CounterTracker = field(default_factory=CounterTracker)
    accepted_counters: list[int] = field(default_factory=list)

    def verify(self, record: dict) -> RecordVerdict:
        """Verify hash, HMAC, signature and counter continuity for one record."""
        try:
            counter = int(record["counter"])
            overrun_cnt = int(record.get("overrun_cnt", 0))
            window_flags = int(record["window_flags"])
            descriptor = descriptor_bytes(
                counter,
                int(record["window_start"]),
                [int(m) for m in record["zc_samples"]],
                window_flags,
                int(record["device_id"]),
                int(record["p_avg_q30"]),
                int(record["energy_uwh"]),
                overrun_cnt,
            )
            prev_hash = bytes.fromhex(record["prev_hash_hex"])
            subs = [
                SubWindow(
                    v_q15=np.array(v, dtype=np.int64),
                    i_q15=np.array(i, dtype=np.int64),
                    valid=bool((int(record["window_flags"]) >> k) & 1),
                )
                for k, (v, i) in enumerate(zip(record["samples_v"], record["samples_i"]))
            ]
            if len(subs) != 5 or len(prev_hash) != 32:
                return RecordVerdict(False, "malformed", counter)
            sig = bytes.fromhex(record["sig_hex"])
            transported = bytes.fromhex(record["descriptor_hex"])
        except (KeyError, ValueError, TypeError):
            return RecordVerdict(False, "malformed", None)
        if transported != descriptor:
            # Transported encoding and rebuilt fields disagree: something in
            # the 36 descriptor bytes (fields, or even the overrun count
            # byte) was altered after signing.
            return RecordVerdict(False, "bad-descriptor", counter)
        # OVERRUN rule (Week 5b): count > 0 IFF flags bit 6. Either
        # mismatch direction means the record misdescribes its own drops.
        if bool((window_flags >> OVERRUN_BIT) & 1) != (overrun_cnt > 0):
            return RecordVerdict(False, "bad-overrun", counter)
        # OVERRUN range (Week 5c item 6): RTL counts tombstone slots, 0..5
        # by construction (5 slots, one drop each). Anything above 5 is
        # forged or misbuilt — rejected before touching keys or samples.
        if overrun_cnt > 5:
            return RecordVerdict(False, "bad-overrun-range", counter)
        continuity = self.tracker.ingest(counter)
        if continuity in ("rejected-replay", "rejected-reorder"):
            return RecordVerdict(False, continuity, counter)
        window_hash = build_window_hash(prev_hash, descriptor, subs)
        if window_hash.hex() != record["window_hash_hex"]:
            return RecordVerdict(False, "bad-hash", counter)
        hmac_tag = compute_hmac(self.hmac_key, descriptor, window_hash)
        if hmac_tag.hex() != record["hmac_hex"]:
            return RecordVerdict(False, "bad-hmac", counter)
        if len(sig) != 64 or not verify_signature(
            self.verify_key, sig, hmac_tag + descriptor + window_hash
        ):
            return RecordVerdict(False, "bad-signature", counter)
        self.accepted_counters.append(counter)
        return RecordVerdict(True, continuity, counter)

    def counters(self) -> list[int]:
        """[counts] accepted window counters in acceptance order."""
        return list(self.accepted_counters)

    def missing(self) -> list[int]:
        """[counts] detected window drops."""
        return self.tracker.missing()
