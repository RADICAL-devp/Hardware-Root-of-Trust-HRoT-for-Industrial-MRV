"""Deterministic key provisioning for simulation (DECISIONS.md D-03).

`provision.py --seed 42` (default `--out keys/`) generates the 256-bit
HMAC key and the Ed25519 keypair deterministically into `keys/`
(gitignored); only fake test vectors in `tests/fixtures/` are committed.
Nothing is hardcoded: both keys derive via SHA-256 domain separation
from the seed, so `make repro` (SEED=42) reproduces every key byte while
zero secrets live in git.

Files written:
- `ed25519_seed.bin` (32 B private seed, mode 0600),
- `hmac_key.bin` (32 B symmetric key, mode 0600),
- `device.json` (`{"device_id", "verify_key_hex"}` — the verifier's trust
  anchor material; the private seed never leaves `keys/` or the SE model).
"""

import argparse
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import SigningKey

ED_SEED_DOMAIN = b"hrot-ed25519-v1"  # [bytes] SHA-256 domain separator, Ed25519
HMAC_DOMAIN = b"hrot-hmac-v1"  # [bytes] SHA-256 domain separator, HMAC key


@dataclass(frozen=True)
class KeyMaterial:
    """Provisioned key material (sizes documented per field)."""

    ed25519_seed: bytes  # [bytes] 32 B Ed25519 private seed (never committed)
    verify_key: bytes  # [bytes] 32 B Ed25519 public key (verifier trust anchor)
    hmac_key: bytes  # [bytes] 32 B HMAC-SHA256 key (FPGA + SE models only)
    device_id: int  # [u32] device identifier assigned at provisioning


def derive_keys(seed: int, device_id: int = 1) -> KeyMaterial:
    """Derive all key material deterministically from `seed` [dimensionless]."""
    tag = ED_SEED_DOMAIN + struct.pack("<q", int(seed))
    ed_seed = hashlib.sha256(tag).digest()
    hmac_key = hashlib.sha256(HMAC_DOMAIN + struct.pack("<q", int(seed))).digest()
    verify_key = bytes(SigningKey(ed_seed).verify_key)
    return KeyMaterial(
        ed25519_seed=ed_seed, verify_key=verify_key, hmac_key=hmac_key, device_id=device_id
    )


def provision_to_dir(seed: int, out_dir: str | Path) -> dict[str, Path]:
    """Write provisioned files into `out_dir`; return paths by file name."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    km = derive_keys(seed)
    paths = {
        "ed25519_seed.bin": out / "ed25519_seed.bin",
        "hmac_key.bin": out / "hmac_key.bin",
        "device.json": out / "device.json",
    }
    paths["ed25519_seed.bin"].write_bytes(km.ed25519_seed)
    paths["hmac_key.bin"].write_bytes(km.hmac_key)
    paths["device.json"].write_text(
        json.dumps({"device_id": km.device_id, "verify_key_hex": km.verify_key.hex()}) + "\n"
    )
    for name in ("ed25519_seed.bin", "hmac_key.bin"):
        paths[name].chmod(0o600)
    return paths


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `provision.py --seed 42 [--out keys]`."""
    parser = argparse.ArgumentParser(description="Deterministic simulation key provisioning.")
    parser.add_argument("--seed", type=int, default=42, help="deterministic seed")
    parser.add_argument("--out", type=str, default="keys", help="output directory (gitignored)")
    args = parser.parse_args(argv)
    paths = provision_to_dir(args.seed, args.out)
    km = derive_keys(args.seed)
    print(f"seed={args.seed} device_id={km.device_id} verify_key={km.verify_key.hex()}")
    for name, path in paths.items():
        print(f"wrote {path} ({name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
