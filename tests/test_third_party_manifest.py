"""Week 5c item 4: third-party RTL manifest (secworks SHA-256).

Every file under rtl/third_party/secworks-sha256/ is pinned by SHA-256
digest: any modification, addition or deletion fails this test. The
upstream commit pin lives in docs/THIRD_PARTY.md (full 40-char hash);
this test asserts it is present and well-formed. Digests below come from
an actual run over the cloned tree (never fabricated); regenerate them
only when the pin itself moves, in the same commit that moves it.
"""

import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TP = REPO / "rtl" / "third_party" / "secworks-sha256"
PINNED_COMMIT = "837c5cc396f001d18f2c765721c585716eb439ae"

MANIFEST = {
    ".gitattributes": "ae042841ee61e7688a90c8556a3ad8bc5cdd27a7105bb49f48ade26d71bc7e34",
    ".github/workflows/ci.yml": "d7fbbe8b195b0edd3f4664016f3779f0afa5d1b070de515337559ab8b0d4ece0",
    ".gitignore": "5a4975fa5b6959461ef722d621440c59656ee603cbcfca431679177923b45056",
    "LICENSE": "943866e8c0449b472dfebe4d10f0641240abb37778165eb4e68200ce1180890d",
    "README.md": "194d0241c7bfdf869bfc516ea31be9adcd5c65f90d07eb4664e9330cfc98f20e",
    "Releases.md": "32eb4c9bf8dba460f6c153537ce94576a94733a618ea8d2ea58b0f2638fb9e23",
    "data/sky130.tcl": "700df50d690e332d8d70a7fd121e89b33107d6d91e040ee9612e17ade7f0df92",
    "sha256.core": "e2ca9b859dda82abd2c2588851dade5b4aa8595d5e9a2ea7a8ae083a2ac749f5",
    "src/interfaces/axi4/rtl/sha256_axi4.v": "508d6d10cd171f05ca2157f8ecd2b8e927ea35522794dc331496f19864a5a9e6",
    "src/interfaces/axi4/rtl/sha256_axi4_slave.v": "54f8ee7f691d1d0bacb58edf792a7085a1c6a4870ba0d7b2282ada4a17d72e6f",
    "src/interfaces/axi4/tb/tb_sha256_axi4.v": "db84eb40e329503be6b0919b50421bdf632940b076e56a343b96cfe2a0d65960",
    "src/interfaces/stream/rtl/sha256_stream.v": "1de98e13481d31d64c258f82385026a08cbf0e7414a2cc46cd1ae1d9f0b9139a",
    "src/interfaces/stream/scripts/pad.py": "d7dd32ebedcde3ebeaef3c09f15b461232093f88bfc586a06a2192dceebe653f",
    "src/interfaces/stream/tb/tb_sha256_stream.v": "861357c8b3734b607868ed651b558cd49b8fa80079739a79ccd2fc6718055552",
    "src/model/sha256.py": "13cbcaf5e3669a10be481d13310e3ea88d5fdfdb6c9d2d232941559210a50689",
    "src/rtl/sha256.v": "091d2338614c2f14dd61f663fe172a912616c76ac4a78c31be7b55e93a89b1cd",
    "src/rtl/sha256_core.v": "81ea4b865b09ab42034ada6acfeaa2ea2880d39918878aaaa816852677840192",
    "src/rtl/sha256_k_constants.v": "78a9600a6968eb0dc04a3a9fdc6329d3b503044b5a2561263b1cdab18f657007",
    "src/rtl/sha256_w_mem.v": "0b4de636364625e34e771bb26363d24f4a4bcae6579d0f8cd3e86d7e5c14354a",
    "src/tb/tb_sha256.v": "ea0b6aefa9d611b3c898d8c6c6d6e44b43925f925653fe76220800827cd04dc0",
    "src/tb/tb_sha256_core.v": "42157958278fa0bcf0a2d4b88e8993460da7f6e05f346ca1924777896cd58637",
    "src/tb/tb_sha256_w_mem.v": "ec9069fa7ddb664a551af3da9ade3f56ad87e36b731ad54a6b258c17d30d6b85",
    "toolruns/Makefile": "94bb5692b540e595d119e63d14d3d7f75cd65fa497f6ea6182a596e3cb32a374",
}


def test_manifest_matches_tree():
    files = sorted(p for p in TP.rglob("*") if p.is_file())
    assert len(files) == len(MANIFEST), (len(files), len(MANIFEST))
    for path in files:
        rel = path.relative_to(TP).as_posix()
        assert rel in MANIFEST, f"untracked third-party file: {rel}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == MANIFEST[rel], f"third-party file changed: {rel}"


def test_pin_recorded():
    assert re.fullmatch(r"[0-9a-f]{40}", PINNED_COMMIT), "pin must be a full 40-char hash"
    text = (REPO / "docs" / "THIRD_PARTY.md").read_text()
    assert PINNED_COMMIT in text, "pinned commit missing from docs/THIRD_PARTY.md"
