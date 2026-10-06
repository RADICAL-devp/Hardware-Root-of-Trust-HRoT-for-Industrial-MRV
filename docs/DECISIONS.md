# DECISIONS — Week 1 Foundations

Source of truth: `docs/BLUEPRINT.md` §4. This file resolves the gaps
flagged in the Week 1 prompt. One-line rationale per decision.
Approved: 1 s window, full 32 B HMAC, ENERGY as µWh (+ raw debug).

## D-01 Telemetry frame: two levels, little-endian, CRC16-CCITT-FALSE

- Endianness: little-endian for all multi-byte fields.
  Rationale: native to x86/ARM/Verilator, avoids RTL byte-swap bugs.
- CRC variant: CRC16-CCITT-FALSE (poly `0x1021`, init `0xFFFF`,
  no refin/refout, xorout `0x0000`).
  Rationale: ubiquitous test vectors, trivial RTL, unambiguous vs Kermit/USB.
- L0 SampleFrame (Edge → FPGA over UART @ 10 kHz, fixed 11 bytes):
  `[SOF 0xA5 u8][counter u32][V i16][I i16][CRC16 u16]`, CRC computed
  over `counter || V || I`.
  Rationale: fixes blueprint §4 `[SOF][counter][V][I][CRC][...]` — the
  `[...]` was the window-level attestation fields, now split into L1.
- L1 AttestationRecord (FPGA → ledger @ 1 Hz, JSONL per blueprint §4
  ledger record): `{counter (= window_id), window_start, E_saved,
  prev_hash, hash (= window_hash), hmac_tag (32 B hex), ed25519_sig
  (64 B hex)}`, plus a fixed 198-byte binary TLV mirror for RTL cosim.
  Rationale: JSONL is auditable evidence; the fixed binary mirror keeps
  RTL parsing deterministic.

## D-02 Signing granularity: FPGA hashes, secure element signs (per window)

- Window: N = 50 whole 50 Hz cycles = 10 000 samples @ 10 kHz = 1.0 s,
  hence 1 Ed25519 signature/second; N stays a named constant so Week 7
  can re-run N = 10 (200 ms) as a sensitivity case.
  Rationale: per-sample Ed25519 at 10 kHz is infeasible; 1 Hz fits any
  secure element while keeping ledger latency at 1 s.
- FPGA (Verilog) streams
  `window_hash = SHA256(prev_hash || window_id || device_id || V[i] ||
  I[i] || P[i] for all i)`, checks L0 CRC, holds the monotonic counter,
  and computes HMAC-SHA256 over header + hash.
  Rationale: hashing plus symmetric ops are cheap in HW; amends blueprint
  "signs each sensor frame", which is unrealistic in Verilog.
- Secure element (Python model) holds the Ed25519 private key, verifies
  HMAC/chain over the local secure channel in simulation, then emits
  `SIG = Ed25519(HMAC || WINDOW_ID || DEVICE_ID || P_AVG || ENERGY_UWH
  || WINDOW_HASH)`.
  Rationale: a single expensive asymmetric op per window, private key
  never leaves the SE.

## D-03 Key provisioning (simulation) + compromised-MPC powers

- Provisioning: `sim/provision.py --seed 42` (Week 3) generates the
  256-bit HMAC key + Ed25519 keypair deterministically into `keys/`
  (gitignored); only fake test vectors in `tests/fixtures/` are committed.
  Rationale: reproducible `make repro` with zero secrets in git.
- A compromised MPC CAN: choose wasteful `u_k`, read/drop/reorder/replay/
  inject post-SE traffic, omit windows, and attempt ledger edit/delete.
  Rationale: models a Dolev-Yao network attacker; matches blueprint "can
  waste energy but cannot forge savings records".
- It CANNOT: forge HMAC/SIG without the keys, rewind WINDOW_ID/ENERGY
  without detection, extract SE/FPGA keys, or mint a valid WINDOW_HASH
  without the sample stream.
  Rationale: keys never leave the SE/FPGA model; the verifier needs only
  the public key.

## D-04 Window length, sample rate, fixed-point formats

- Sample rate 10 kHz (200 samples/cycle @ 50 Hz nominal); 12-bit ADC
  plus noise plus jitter from day one.
  Rationale: integer samples per cycle per AGENTS.md sensor rule.
- Attestation window 1.0 s = 50 cycles = 10 000 samples.
  Rationale: 50-cycle averaging gives stable `P = mean(v·i)` and maps 1:1
  to ledger/CO₂ rows.
- Formats: V: i16 Q15 normalized to full-scale (e.g. 500 Vpk),
  I: i16 Q15 normalized to full-scale (e.g. 100 Apk); P_inst/P_avg:
  i32 Q30 (`V*I`, exact); ENERGY ledger: u64 µWh monotonic, plus an
  optional u64 raw accumulator for debug; verifier allows 1-LSB tolerance.
  Rationale: Q15×Q15→Q30 is exact in 32 bits; the ledger stays readable
  while the RTL stays bit-checkable against the Python golden model.

## D-05 Known limitation (Week 2, measured): fixed whole-cycle windows under grid drift

- Finding (actual run, SEED=42, `results/metrics.json`, steady profile):
  fixed 200-sample windows assume exactly 50 Hz and the window logic is
  never given the true frequency. The sensor-chain error (measured vs true
  under identical windows) is flat across drift (~0.10 % mean P — the
  AFE/ADC path is drift-independent). But the fixed-window bias vs the
  coherent true-frequency reference grows ~linearly with |df|: P mean
  0.28 % at ±0.2 Hz (default evaluation point), 0.70 % mean / 1.1 % max at
  ±0.5 Hz; Vrms window bias up to ~0.50 % max at ±0.5 Hz.
  Rationale for reporting, not fixing: at ±0.5 Hz the bias (~0.7 % mean) is
  the same order as the sensor error budget (acceptance bound 1.506 %),
  so it is material and must stay visible.
- Status: known limitation, NOT fixed in Week 2. No cheating: the device
  path still uses fixed windows; only the offline characterization
  reference uses plant ground truth.
- Proposed mitigation (needs approval): zero-crossing-aligned windows —
  detect voltage rising-edge zero crossings in the FPGA, accumulate whole
  true cycles (variable sample count per window), and record the actual
  sample count per window in the attestation record so the verifier can
  re-derive P/RMS exactly. Cost: variable-latency windows and a small
  amount of extra RTL (edge detector + counter); benefit: drift bias
  removed down to residual jitter (~1 µs rms, negligible at 50 Hz).
