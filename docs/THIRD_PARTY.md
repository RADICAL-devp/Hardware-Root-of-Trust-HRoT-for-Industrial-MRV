# Third-party RTL

Source of truth for provenance: this file. The wrapped core is NEVER
modified — our logic lives OUTSIDE `rtl/third_party/`.

## secworks SHA-256 (`rtl/third_party/secworks-sha256/`)

- Upstream: `https://github.com/secworks/sha256` by Joachim Strömbergson.
- Pinned commit: `837c5cc396f001d18f2c765721c585716eb439ae`
  (2025-12-15, merge PR #26). Cloned full, then the nested `.git` was
  removed so this tree is self-contained; re-clone + `git rev-parse HEAD`
  reproduces the pin.
- License: BSD 2-Clause — see `rtl/third_party/secworks-sha256/LICENSE`
  (© 2013 Joachim Strömbergson; per-file headers add Secworks Sweden AB).
  Redistribution in source form retains the copyright notice (this file +
  untouched upstream headers satisfy the condition).
- Files we instantiate (read-only): `src/rtl/sha256_core.v` (the core),
  `src/rtl/sha256_w_mem.v` + `src/rtl/sha256_k_constants.v` (its
  submodules). We do NOT use the `sha256.v` memory-mapped top nor the
  `src/interfaces/` (stream/AXI4) wrappers.
- What it does / does not do (per upstream README): 66 cycles/block,
  SHA-256 + SHA-224 capable (we tie SHA-256 only), NO padding of the
  final block (our wrapper owns 0x80/length encoding).
