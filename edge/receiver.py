"""Pure-Python reference receiver (Week 3 test harness, not the Week 5 verifier).

L0 stream policy: `counter` values form aSeen set with a `max_seen`
watermark. An already-seen counter is REJECTED as replay; an unseen
counter below the watermark is ACCEPTED but flagged as reordered
(buffered disorder, never silently ordered); a jump past `max_seen + 1`
is accepted with a recorded gap; end-of-stream counters in
`[0, max_seen]` absent from the set are drops — reported via `missing()`,
never silent.

Record policy: the receiver rebuilds the descriptor bytes from the
record's integer fields (never trusting transported encodings),
recomputes the window hash from the transported samples, checks the HMAC
in constant time, then the Ed25519 signature against the provisioned
public key. An invalid-flagged window verifies exactly like a valid one
and advances the window counter: flags are records, not drops.
"""

from dataclasses import dataclass, field

import numpy as np

from edge.attestation import SubWindow, build_window_hash, compute_hmac, descriptor_bytes
from edge.framing import Frame
from edge.secure_element import verify_signature

OK = ("ok", "ok-gap", "ok-reordered")


@dataclass
class CounterTracker:
    """Monotonic-counter continuity tracker shared by both receiver levels."""

    seen: set[int] = field(default_factory=set)
    max_seen: int | None = None
    gaps: list[tuple[int, int]] = field(default_factory=list)
    reorders: list[int] = field(default_factory=list)

    def ingest(self, counter: int) -> str:
        """Ingest a counter; return ok | ok-gap | ok-reordered | rejected-replay."""
        counter = int(counter)
        if counter in self.seen:
            return "rejected-replay"
        self.seen.add(counter)
        if self.max_seen is None or counter == self.max_seen + 1:
            self.max_seen = counter
            return "ok"
        if counter > self.max_seen + 1:
            self.gaps.append((self.max_seen + 1, counter - 1))
            self.max_seen = counter
            return "ok-gap"
        self.reorders.append(counter)
        return "ok-reordered"

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
    def reorders(self) -> list[int]:
        """[counts] late-but-unseen counters accepted out of order."""
        return self.tracker.reorders

    def missing(self) -> list[int]:
        """[counts] detected drops (see `CounterTracker.missing`)."""
        return self.tracker.missing()


@dataclass
class RecordVerdict:
    """Outcome of one attestation-record verification."""

    accepted: bool  # [flag] True iff hash + HMAC + signature (+ continuity) hold
    reason: str  # [text] ok | ok-gap | ok-reordered | rejected-* | malformed | bad-*
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
            descriptor = descriptor_bytes(
                counter,
                int(record["window_start"]),
                [int(m) for m in record["zc_samples"]],
                int(record["window_flags"]),
                int(record["device_id"]),
                int(record["p_avg_q30"]),
                int(record["energy_uwh"]),
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
        except (KeyError, ValueError, TypeError):
            return RecordVerdict(False, "malformed", None)
        continuity = self.tracker.ingest(counter)
        if continuity == "rejected-replay":
            return RecordVerdict(False, "rejected-replay", counter)
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
