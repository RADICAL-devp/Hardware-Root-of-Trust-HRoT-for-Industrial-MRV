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

## D-05 RESOLVED (Week 2b, measured): zero-crossing-aligned windows

- Result (actual run, SEED=42, `results/metrics.json` → `zc_windows`,
  steady profile WITH noise + 3 %/1 % 3rd/5th harmonics + 2.0 V DC offset):
  fixed-window P bias at ±0.5 Hz is 0.66–0.68 % mean / 1.11–1.13 % max;
  zero-crossing windows give 0.10 % mean / 0.13–0.14 % max, flat across
  the whole ±0.5 Hz sweep with 100 % windows valid — ~8x better on max
  error, back at the sensor-noise floor. No tuning: thresholds and bounds
  below were fixed before the numbers were read.
  Rationale: the improvement is material (bias removed down to truncation
  + noise), so the mitigation is adopted.

### Window definition

- Measurement window: N = 10 whole grid cycles (default), variable sample
  count M. Rationale: 10 cycles ≈ 200 ms nominally, matching the Week 7
  sensitivity scale already named in D-02; long enough that ≤1-sample
  boundary truncation (≤0.1 %) stays below the sensor floor, short enough
  for 5 sub-windows per 1 s attestation record.
- Boundaries ONLY from rising zero crossings on the measured, quantized
  voltage: arm when `v < -5 V` (hysteresis ≈ 14x noise rms; symmetric
  gating so it rejects chatter without shifting the crossing), trigger on
  the first `v >= 0` sample. A linear-interpolated fractional position is
  reported for diagnostics; window sums use the integer detection indices,
  exactly what the RTL counter produces. Rationale: comparators + counter
  only — no divider in the detection path; golden model stays
  bit-comparable with RTL.
- The plant's true frequency is never used by the window logic (enforced
  by `tests/test_week2b_windows.py`, which passes only measured samples).

### Supported range and validity

- Supported grid-frequency range: 45–55 Hz (±10 %, covers all normal and
  credible contingency drift; evaluation sweeps ±0.5 Hz).
  Rationale: ±10 % keeps the reciprocal-LUT M range to 421 values (one
  512-entry table) while staying far outside any test condition.
- Per-cycle gap must lie in [150, 260] samples (catches missing crossings
  from dropouts → ~2x gap, and extra crossings from severe distortion).
- Window sample count must lie in M ∈ [1810, 2230] samples, derived as
  [N·fs/f_max − 8, N·fs/f_min + 8] = [1818 − 8, 2222 + 8]; the ±8 margin
  covers jitter/noise boundary shifts of a few samples.
  Rationale: gap check = consistency, M check = range; both derived from
  the range above, no magic numbers.
- Any violation → window INVALID: excluded from power means (NaN in the
  golden model, never a number) and flagged upstream, never silently
  healed — consistent with the threat-model drop policy.

### Ledger record change (D-01 L1, applies from Week 3/5)

- JSONL attestation record gains `window_flags` u8 (bitmask over the 5 ZC
  sub-windows in the 1 s record; bit = 1 → sub-window valid) and
  `zc_samples` (array of 5 × u16 window counts M, 0 when invalid).
  The fixed 198-byte binary TLV mirror grows by 12 bytes (1 + 5×2 +
  1 reserved; exact layout recomputed in Week 5).
  Rationale: the verifier needs M per sub-window to re-derive P/RMS
  exactly, and the flags to exclude invalid sub-windows from savings
  (policy: flagged, not healed — invalid sub-windows contribute no energy
  either way; Week 5 `verifier.py` implements this).
- Attestation rate stays 1 Hz (D-02 unchanged): the FPGA accumulates 5
  consecutive ZC sub-windows per record; the SE still signs once per
  record. L0 sample frames are UNCHANGED.

### RTL variable-count mean (no RTL written yet; Week 4 target)

- Recommended: 512-entry reciprocal LUT, 9-bit index (`M − m_min`),
  24-bit fractional output; one multiply-shift per window:
  `P_avg = (sum_Q30 * LUT[M]) >> 24`. Cost: one 18-Kb BRAM (512×24 b =
  12 Kib) or ~300 distributed LUTs; single-cycle; measured worst-case
  error 0.0066 % over the full M range (~230x below the sensor budget).
- Rejected alternative 1: 18-bit LUT output — measured 0.41 % worst-case,
  material vs the 1.506 % sensor bound (too few significant bits for
  1/M ≈ 1/2000). Rationale for showing this: the sizing is measured.
- Rejected alternative 2: non-restoring divider — bit-exact but ~300 LUTs
  plus ~20 cycles latency for zero accuracy benefit at this budget.
- Fallback alternative 3: FPGA emits (sum, M), division in the SE
  software — zero HW cost and the verifier recomputes anyway, but it
  moves averaging out of the attested core; kept only if LUT BRAM is
  needed elsewhere (Week 5 Yosys numbers decide).

### Golden-model plan (Week 4)

- `sensors/windows.py` IS the golden model: integer-bound sums are the
  bit-exact RTL match target (1-LSB tolerance per AGENTS.md); float
  division is the characterization reference; `reciprocal_lut_inv` /
  `lut_window_mean` model the fixed-point path for accuracy analysis.
- Week 4 cocotb vectors become variable-M (M drawn from [1810, 2230]
  plus out-of-range M asserting the invalid flag); `power_calc` takes
  (sample stream, window strobe, M) and its P_avg register must match
  `lut_window_mean` bit-for-bit.
- Week 7 sensitivity re-runs N ∈ {5, 20} plus a fixed-window ablation
  (reproduces the D-05 bias) instead of the old N = 10 case, which is
  now the default.

## Week 3: signed-message byte layout (D-02/D-05 scoped amendment)

The ONLY change to prior decisions: D-02's SIG preimage now explicitly
carries the four D-05 descriptor fields (`counter`, `window_start`,
`zc_samples`, `window_flags`). Signing granularity (1 Hz, SE signs,
FPGA hashes + HMACs), frame layout, CRC variant, endianness, key
provisioning and fixed-point formats are all unchanged.

- Signed preimage: `SIG = Ed25519(HMAC || descriptor || WINDOW_HASH)`,
  all integers little-endian, total 100 bytes:

  | off | size | field |
  |-----|------|-------|
  | 0 | 32 | HMAC = HMAC-SHA256(hmac_key, descriptor \|\| window_hash) [bytes] |
  | 32 | 4 | counter (= window_id, D-01) [u32] |
  | 36 | 4 | window_start = L0 counter of the window's first sample [u32] |
  | 40 | 10 | zc_samples = 5 × u16 window counts M, 0 when invalid [u16×5] |
  | 50 | 1 | window_flags, bit k = sub-window k valid [u8] |
  | 51 | 1 | reserved = 0x00 [u8] |
  | 52 | 4 | device_id [u32, assigned at provisioning, default 1] |
  | 56 | 4 | P_AVG [i32 Q30 = mean(v_q15·i_q15) over valid samples only] |
  | 60 | 8 | ENERGY_UWH [u64 cumulative measured µWh, valid sub-windows only] |
  | 68 | 32 | WINDOW_HASH [bytes] |

  Rationale: every field the verifier re-derives (M per sub-window, which
  sub-windows count, P_AVG, energy) is inside the signature; anything an
  attacker flips (`window_flags`, `zc_samples`) breaks verification.
- descriptor = preimage bytes 32–68 (36 bytes).
  Rationale: one named byte string shared by hash, HMAC and signature code.
- `WINDOW_HASH = SHA256(prev_hash || descriptor || samples)`, where
  samples = `V_q15 || I_q15 || P_inst_q30` per sample over ALL samples of
  all 5 sub-windows, valid or not.
  Rationale: resolves the D-02 "for all i" ambiguity; invalid sub-windows
  (e.g. dropout zeros) are data like any other, distinguished by the
  signed flags.
- Q15 encoding pinned exact: `q15 = clip(round(x / FS · 2^15), −32768,
  32767)` with V_FS = 500 Vpk, I_FS = 100 Apk (D-04 "e.g." values, now
  exact); `P_inst_q30 = v_q15 · i_q15` (exact, full-scale 50 kW).
  Rationale: the L0 i16 fields carry these codes directly, so Week 4 RTL
  needs no conversion.
- ENERGY_UWH accumulates measured energy over valid sub-windows only:
  `E += round(P_avg_W · M / fs / 3600 · 10^6)`; invalid sub-windows add 0.
  Rationale: flagged energy is excluded from savings, never healed, per
  the D-05 policy (E_saved baselines arrive Week 6).
- prev_hash genesis = 32 zero bytes; the chain is carried from Week 3 but
  enforced (reorder/edit/delete detection) in Week 5 `verifier.py`.
  Rationale: forward-compatible record shape without claiming Week 5
  guarantees early.
- Receiver policy (reference, Week 3): L0 `counter ≤ max_seen and seen`
  → reject as replay; unseen-but-late counter → accept + reorder flag;
  jump → accept + gap record; end-of-stream missing counters = drops,
  reported, never silent. Invalid-flagged windows verify normally and
  advance the counter (they are records, not drops).
  Rationale: matches the Week 3 test list; Week 7 replays this policy
  under attack traffic.
