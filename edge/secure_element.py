"""Simulated secure element holding the Ed25519 private key (DECISIONS.md D-02).

Trust boundary: the SE owns the device Ed25519 private key and the HMAC
key (shared with the FPGA model per D-03). It verifies the FPGA's HMAC
over `(descriptor || window_hash)` with a constant-time comparison, then
emits `SIG = Ed25519(HMAC || descriptor || WINDOW_HASH)` (100-byte
preimage, Week 3 byte-layout note). The private key is NEVER exposed
through any public method: the API surface is `{attest, public_key}`
plus constructors, and `attest` is a signing oracle over caller-supplied
bytes (callers cannot extract key material through it).

Compromised MPC (D-03) sees only signatures and the public key; without
the keys it cannot forge HMAC/SIG, rewind counters, or mint windows.
"""

from dataclasses import dataclass

from nacl.signing import SigningKey, VerifyKey

from sim.provision import KeyMaterial, derive_keys

DESCRIPTOR_LEN = 36  # [bytes] signed descriptor length (Week 3 layout note)
HASH_LEN = 32  # [bytes] SHA-256 window hash length
HMAC_LEN = 32  # [bytes] HMAC-SHA256 tag length
SIG_LEN = 64  # [bytes] Ed25519 signature length


class SEError(Exception):
    """Secure-element refusal (HMAC mismatch or malformed inputs)."""


@dataclass
class SecureElement:
    """Simulated secure element; construct via `from_seed` / `from_key_material`."""

    _signing_key: SigningKey
    _hmac_key: bytes
    _verify_key: bytes

    @classmethod
    def from_seed(cls, seed: int) -> "SecureElement":
        """Build the SE from a provisioning seed [dimensionless]."""
        return cls.from_key_material(derive_keys(seed))

    @classmethod
    def from_key_material(cls, km: KeyMaterial) -> "SecureElement":
        """Build the SE from provisioned key material (private key stays inside)."""
        return cls(
            _signing_key=SigningKey(bytes(km.ed25519_seed)),
            _hmac_key=bytes(km.hmac_key),
            _verify_key=bytes(km.verify_key),
        )

    @property
    def public_key(self) -> bytes:
        """Return the 32-byte Ed25519 verify key [bytes] (safe to publish)."""
        return bytes(self._verify_key)

    def attest(self, descriptor: bytes, window_hash: bytes, hmac_tag: bytes) -> bytes:
        """Verify HMAC, then sign `HMAC || descriptor || WINDOW_HASH`.

        Args:
            descriptor: 36-byte signed descriptor [bytes].
            window_hash: 32-byte SHA-256 window hash [bytes].
            hmac_tag: 32-byte FPGA HMAC tag [bytes].

        Returns:
            64-byte Ed25519 signature [bytes].

        Raises:
            SEError: HMAC mismatch or malformed input lengths.
        """
        import hashlib
        import hmac as hmac_mod

        if len(descriptor) != DESCRIPTOR_LEN:
            raise SEError(f"descriptor must be {DESCRIPTOR_LEN} bytes")
        if len(window_hash) != HASH_LEN or len(hmac_tag) != HMAC_LEN:
            raise SEError("window_hash and hmac_tag must be 32 bytes each")
        expected = hmac_mod.new(self._hmac_key, descriptor + window_hash, hashlib.sha256).digest()
        if not hmac_mod.compare_digest(expected, bytes(hmac_tag)):
            raise SEError("HMAC mismatch: FPGA tag rejected, nothing signed")
        preimage = bytes(hmac_tag) + bytes(descriptor) + bytes(window_hash)
        return bytes(self._signing_key.sign(preimage).signature)


def verify_signature(verify_key: bytes, signature: bytes, message: bytes) -> bool:
    """Verify an Ed25519 signature [bytes]; True iff valid (never raises)."""
    from nacl.exceptions import BadSignatureError

    try:
        VerifyKey(bytes(verify_key)).verify(bytes(signature) + bytes(message))
        return True
    except (BadSignatureError, ValueError):
        return False
