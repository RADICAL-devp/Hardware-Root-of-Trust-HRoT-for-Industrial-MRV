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
  I[i] || P[i] for all i)` (component order amended Week 3, reordered
  Week 5c — see the Week 3 byte-layout note), checks L0 CRC, holds the monotonic counter,
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

## Week 4a: golden fixed-point spec (tb/golden.py; RTL in Week 4b)

- Rounding mode, everywhere: ROUND HALF AWAY FROM ZERO (means, quotient,
  energy). Rationale: symmetric for ±quotients, so long-run averaging
  carries no DC bias (unlike round-half-up); differs from Python
  banker's round only on exact halves, by 1 LSB. `edge/attestation.py`
  was switched to the same canonical helpers, so the Week 3 record path
  and the golden agree exactly (was: float/banker's, differed ≤1 LSB on
  exact halves — inside tolerance, but two truths are worse than one).
- Accumulator widths (minimum signed): `sum(v·i)` and `sum(v²)` each
  |sum| ≤ 2230·2^30 < 2^42 → 43-bit signed PER SUB-WINDOW (corrected in
  Week 4a3: the record-level P_AVG sum over 5 sub-windows needs 45-bit
  signed — see below; RTL: 64-bit registers throughout, no saturation
  possible); energy product `p_sum·12500` < 2^56 (RTL: 56-bit,
  then shift 30 + divide by 9). Per-sample Q15×Q15 products are exact in
  i32 (full-scale product fits; asserted in the golden).
- ENERGY_UWH increment, integer-exact (no float):
  `E = round-half-away(p_sum · 12500 / (9 · 2^30))` [µWh], derived from
  `P_W·M/fs/3600·10^6` with P_W = p_sum/M/2^30·50000 and fs = 10000.
  Rationale: bit-exact between golden big-ints and the RTL constant
  divider; the fs value is baked in (attestation asserts fs = 10 kHz).
- Vrms/Irms: `isqrt_round_half_up(mean)` — `rem > root → +1`, matching a
  non-restoring RTL square-root unit bit-for-bit; u16 Q15 output with one
  documented saturating corner (full-negative DC → root 32768 → 32767).
  PF: `round-half-away(P_AVG·2^15 / (Vrms·Irms))`, i16 Q15 signed,
  saturated, 0 when the denominator is 0.
- UART split: the golden parses the BYTE stream (SOF hunt, CRC gate,
  resync — cross-tested vs edge.framing); bit-level line signaling
  (start/stop sampling, baud error) is RTL-only, driven by cocotb in
  Week 4b against golden bytes/frames.
- Detector RTL mapping (new module `zc_detect.v` in Week 4b alongside
  `uart_rx.v`/`power_calc.v`): hysteresis = two comparators (arm at
  v < −5 V in codes, trigger first v ≥ 0 while armed); detection index =
  counter value (window boundary, no division); fractional interpolation
  stays diagnostics-only; gap/M validation = range comparators; invalid →
  M recorded 0 with sums forced 0 (never a plausible number).
- Week 4b targets: 1,000 seeded vectors with max/mean LSB error reported,
  all directed cases, reciprocal-LUT sweep over all 421 M values, VCD on
  any failure without loosening the 1 LSB bound; Verilator 5.048 present,
  iverilog present (`brew install icarus-verilog` only if cocotb needs the
  `icarus` shim — iverilog binary already ships with it).

## Week 4a2: review fixes (integer detector, NEG_ENERGY, contracts)

- Integer-exact detector: `find_rising_crossings_q15` on Q15 codes is the
  single source of truth — arm when `code < ARM_Q15` (-328 codes),
  trigger on the first `code >= 0` while armed. Volts equivalents:
  ARM = -328/32768·500 = **-5.0049 V**, trigger 0 V; hysteresis magnitude
  unchanged (5 V nominal; on the ADC grid the arm level is provably
  identical to the old float rule — pinned by test, not asserted).
  The volts function is a thin wrapper via the exact ADC→Q15 map
  (replica pinned equal to `edge.framing.q15_encode` exhaustively over
  all 4096 ADC codes; no `edge` import — that would cycle with
  `sensors`). Wrapper == core proven on 200 seeded signals spanning
  frequency/amplitude/harmonics/DC/noise/dropouts. Fractional positions
  stay float but diagnostics-only (garbage-`frac` test pins that bounds
  derive from detection indices alone).
- `zc_detect.v` confirmed as the third Week 4b module. Interface
  (sample-rate domain): in `sample_valid`, `v_q15/i_q15` (i16),
  `counter_in` (u32); out `cross_strobe` (1-clk pulse on detection),
  `win_start_pulse`, `M[11:0]` (2230 < 4096), `win_valid`,
  `win_end_strobe`. `power_calc` accumulates per-sample sums gated by the
  strobes and latches outputs 3 clocks after the last sample
  (accumulate → LUT multiply-shift → register; exact latency measured in
  Week 4b). Boundary convention: window covers samples [S, E) — S (the
  detection index) included, E (Nth-later detection) excluded and owned
  by the next window; M = E − S by counter subtraction.
- Interpolation measured, not assumed (+0.5 Hz drift, Week 2b stress
  bundle): integer-boundary residual 0.0989 % mean / 0.1386 % max vs
  fractional-endpoint 0.0989 % mean / 0.1261 % max. The residual is the
  sensor floor, not truncation — interpolation buys ~0.01 pp and stays
  out of the RTL.
- ENERGY bounds: full-scale window (50 kW × 2230 samples) =
  3,097,033 µWh (~3.1 Wh); u64 headroom ≈ 6.0×10¹² windows (~37,700 yr
  at 5 windows/s) — overflow is not a design concern.
- NEG_ENERGY policy (new): a record whose energy total would go
  negative (possible only on synthetic/adversarial data) clamps
  `ENERGY_UWH` to 0 and sets `window_flags` bit 5; `P_AVG` stays
  signed-exact; clamped records still verify. Fail-loud was rejected: a
  Python exception has no RTL equivalent, and one bad window must never
  halt attestation (DoS risk). The flags flip test already sweeps all 8
  bits including bit 5. Week 5 verifier note: on NEG_ENERGY, exclude the
  record's energy from savings and flag it.
- Sum-width derivation: max |per-sample product| = 32768·32767, so over
  M ≤ 2230, max |sum| = 2,394,371,194,880 < 2^42 (and ≥ 2^41) → 42
  magnitude bits + sign = **43-bit signed minimum** (RTL: 64-bit, no
  saturation possible); same bound covers sum(v²). Full-negative-DC
  clamp directed test exists (`test_saturation_corners`: −32768 DC →
  Vrms 32767, the single 1-LSB corner).
- PF contract: 1-LSB RTL-match vs the golden ONLY — PF is not in the
  descriptor, the signature, or the hash; it feeds MPC/baseline
  downstream (Weeks 6–7). P_AVG/ENERGY/zc_samples/window_flags are the
  byte-exact set (ENERGY/P_AVG within the 1-LSB bound, the rest exact).

## Week 4a3: width + policy fixes

- Record-level width correction: the Week 4a "43-bit signed" figure is
  per sub-window. The record P_AVG sum spans up to 5 valid sub-windows
  (11,150 samples): max |sum| = 11,150·32768·32767 = 11,973,085,974,400
  < 2^44 (and ≥ 2^43) → 44 magnitude bits + sign = **45-bit signed
  minimum** (RTL: 64-bit; the per-window `power_calc` sums stay 43-bit,
  the 5-window aggregation gains 3 bits). Pinned by a full-scale
  all-5-valid directed test on the record total (both signs).
  Record ENERGY_UWH max = 5 × 3,097,033 = 15,485,165 µWh (~15.5 Wh);
  u64 headroom ≈ 1.19×10¹² records (~37,700 yr at 1 Hz).
- NEG_ENERGY confirmations: clamp-at-0 + flags bit 5 + signed-exact
  P_AVG policy stands (Week 4a2). Added since: (a) post-signing flips of
  bit 5 rejected in BOTH directions (set-on-healthy, clear-on-clamped —
  the all-8-bits sweep already covered set-direction; now explicit);
  (b) a clamped-window counter — each record carries
  `neg_energy_clamped: bool` metadata (mirror of flags bit 5, outside
  the descriptor/hash like the sample arrays), and aggregation callers
  count it (pinned: 5 clamped of 20 mixed records). Week 5 verifier
  note extended: on NEG_ENERGY, exclude the record's energy from savings,
  flag it, AND count clamped records for monitoring (repeated clamping
  is sensor-fault/attack-indicative, not a savings signal).
- Threshold rounding stated: pre-4a2 float rules were arm `v < -5.0 V`
  and trigger `v[k-1] < 0.0 ≤ v[k]`; the integer rules ARM_Q15 = -328
  and TRIG_Q15 = 0 are their intended rounding to Q15 codes
  (`round(-5/500·32768)`, `round(0/500·32768)` — pinned by test).
- PF contract decided: Week 4b requires EXACT RTL == golden for PF, not
  1 LSB (supersedes the Week 4a "1-LSB match" language for PF only).
  Achievable without new hardware thinking: the PF formula is pure
  integer arithmetic over already-rounded inputs (no LUT in the path),
  so bit-exactness is a testable property, not an aspiration. P_AVG
  keeps its dual reference (exact mean for characterization,
  `lut_window_mean` as the LUT-path RTL target); Week 4b states which
  register each test compares.

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
  The receiver rebuilds it from the record's integer fields AND
  cross-checks the transported encoding; either direction of tampering —
  fields or encoding bytes, including the reserved byte — is rejected
  (`bad-descriptor`).
- `WINDOW_HASH = SHA256(prev_hash || samples || descriptor)` (Week 5c
  reorder, was `prev_hash || descriptor || samples`), where
  samples = `V_q15 || I_q15 || P_inst_q30` per sample over ALL samples of
  all 5 sub-windows, valid or not.
  Rationale: resolves the D-02 "for all i" ambiguity; invalid sub-windows
  (e.g. dropout zeros) are data like any other, distinguished by the
  signed flags. The reorder is a STREAMING necessity, not a security
  change: the descriptor holds P_AVG (mean over all samples) and
  ENERGY_UWH (cumulative over the window), both computable only AFTER the
  last sample — so descriptor-first forces the FPGA to buffer all 80,068
  message bytes before starting the hash, while descriptor-last streams
  from sample 0 (prev_hash → samples as they arrive → descriptor at
  window end). Same three components bound; extension-attack posture
  unchanged (the verifier recomputes the exact fixed-layout preimage, so
  no longer message verifies either way). The two-stage alternative —
  `SHA256(prev_hash || descriptor || SHA256(samples))` — was REJECTED:
  identical binding at the cost of a second full hash pass plus a 32-byte
  digest register in RTL, for zero gain once the order swap already
  streams. SIG preimage (`HMAC || descriptor || WINDOW_HASH`) and HMAC
  input (`descriptor || window_hash`) layouts are UNCHANGED — only the
  hash-internal component order moved, so `SecureElement` and the HMAC
  code are untouched.
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
- Receiver policy (reference, Week 3): L0 `counter` already seen →
  reject as replay; STRICT REJECT for disorder — an unseen counter below
  the watermark is dropped (never added to the seen set), counted in
  `rejected_reorders`, and surfaces as a gap via `missing()`; a jump is
  accepted with a gap record; end-of-stream missing counters = drops,
  reported, never silent. A duplicate of an accepted counter stays
  `rejected-replay` (distinct verdict from `rejected-reorder`).
  Rationale: UART is in-order so disorder is attack-indicative; nothing is
  silently reordered. NOTE: Week 3 planning initially selected
  accept-and-flag for reorders; this strict-reject choice supersedes it
  (approved post-review). Invalid-flagged windows verify normally and
  advance the counter (they are records, not drops).
  Rationale: matches the Week 3 test list; Week 7 replays this policy
  under attack traffic.

## Week 4b2: review fixes (behavioral latency, descriptor source, record RTL)

- Latency is BEHAVIORAL (Week 4b review): `power_calc`'s divider
  (`udiv64`/`div_half_away`) and square root (`isqrt_ru`) are combinational
  loops evaluated inside one clock cycle — exact in simulation, but not a
  hardware timing claim. The reported "3 cycles" (last-sample → `out_valid`)
  is the behavioral-simulation pipeline depth only. Week 5 converts both
  units to iterative FSMs (restoring divide ~64 cycles worst case,
  restoring sqrt 32 cycles) and re-states the true cycle count; until then
  no hardware latency is reported. Rationale: claiming synthesis timing
  from combinational `$`-free loops would be fabrication.
- Descriptor source DECIDED: `p_avg_exact` (round-half-away integer mean)
  is the ONLY P_AVG that feeds the attestation descriptor — it is exactly
  what `edge/attestation.py` and `tb/golden.py::record_fields` compute, so
  the Week 5 verifier re-derives it from samples with no LUT knowledge.
  `p_avg_lut` is characterization only, behind RTL parameter
  `LUT_MEAN_ENABLE` (0 ties the register to 0; synthesis trims ROM +
  multiplier in exact-only builds). This supersedes the Week 4a "dual
  reference" language for the descriptor path: exact is normative, LUT is
  advisory. Rationale: the signed value must be re-derivable by a
  verifier that never sees the FPGA netlist.
- Record-level NEG_ENERGY clamp now exists in RTL (`rtl/record_agg.v`):
  per `power_calc out_valid` it accumulates SIGNED energy increments over
  valid sub-windows only (invalid add 0), adds `energy_prev_in` once, and
  clamps ONCE — total < 0 → `rec_energy` 0 + `rec_flags` bit 5, else the
  total; `rec_p_sum`/`rec_m_total` stay signed-exact for the descriptor
  mean (whose division is Week 5 scope, like the divider FSMs).
  Rationale: the review correctly noted the clamp lived in Python only;
  a Python exception/clamp has no hardware meaning until RTL performs it.
- Trailing `win_start_pulse` CONFIRMED as original spec (not RTL drift):
  Week 4a2 already states "E (Nth-later detection) excluded and owned by
  the next window" — ownership requires the next window to OPEN at E, so
  every `win_end_strobe` coincides with the next `win_start_pulse` (the
  closing E sample is excluded from the old sums and included in the new
  ones in the same edge). The golden `build_windows` emits only completed
  windows, so testbenches expect starts == golden starts + last golden end
  (+ a lone first detection when no window closed yet). Rationale: written
  here so Week 5 `top.v` wiring cannot "fix" it away.
- Clock/baud accounting (simulation): TB clock 10 ns (100 MHz, sim-only);
  `uart_rx` `CLK_PER_BIT` = 16 → 160 ns/bit = 6.25 Mbps sim line rate.
  Minimum line rate for the L0 stream: 11 bytes × 10 bits × 10 kHz =
  1.1 Mbps — a 115200-baud link CANNOT carry raw 10 kHz L0 traffic (order
  of magnitude short; buffering/aggregation would be a redesign, not a
  setting). Baud tolerance is measured, not assumed (Week 4b2, identical
  in both sims): -3.5 % passes / -4 % fails, +5 % passes / +5.5 % fails;
  the pinned ±2 % tolerance keeps >= 1.5 pp margin on the tight (fast)
  side. Rationale: the numbers constrain Week 5 system integration, not
  just the testbench.
- Week 5 scope grows `rtl/frame_rx.v`: byte-stream L0 framer (SOF hunt,
  11-byte assembly, CRC16-CCITT-FALSE gate, resync, frame outputs
  counter/V/I + `frame_valid`/`crc_err`), fed by `uart_rx` bytes and
  proven against `parse_l0_stream`. Rationale: the byte→frame half
  currently lives in the cocotb TB; attestation needs it in hardware.

## Week 5a: frame_rx contract (done)

- `rtl/frame_rx.v` takes `(data_in[7:0], data_valid 1-clk/byte)` as
  `uart_rx` would emit and outputs the latched L0 fields
  (`frame_counter` u32, `frame_v`/`frame_i` i16 bit patterns) with
  `frame_valid` (good CRC) / `crc_err` (bad CRC, frame dropped) 1-clk
  pulses. Garbage before SOF is ignored; trailing partials stay buffered;
  a bad-CRC candidate rescans its bytes 1..10 for the first inner SOF and
  reuses that tail as the next prefix — one-step equivalent of the
  golden's +1 advance (skipped bytes are provably non-SOF), proven
  byte-exact vs `parse_l0_stream` on good/back-to-back, garbage prefix,
  truncated tail, bad CRC, SOF-in-payload, cut-mid-frame and 300-frame
  seeded e2e runs under both Verilator and Icarus.
  Rationale: explicit case-statement resync (no variable-index writes)
  keeps Yosys inference to plain registers.
- Counter continuity (replay/reorder) is NOT checked in `frame_rx` —
  that stays the Python receiver/verifier's job (`edge/receiver.py`,
  Week 5 `ledger/verifier.py`). Rationale: the framer is a byte pipe;
  policy lives where the keys are.
- Deferred to later Week 5 steps (unchanged defaults in this step):
  UART `CLK_PER_BIT` from the 12 MHz target (1.5 Mbaud → 8 clocks/bit)
  with re-measured baud tolerance; divider/sqrt FSM latency plan;
  secworks SHA-256 + HMAC; hash chain + monotonic counter + `top.v`;
  Yosys LUT/FF/BRAM counts with `LUT_MEAN_ENABLE=0`.

## Week 5a2: frame_rx review fixes (done)

- Resync timing / overrun (measured by construction, pinned by test):
  `rescan_pos` is combinational and the prefix shift completes in the SAME
  completion edge as `crc_err`, so the worst-case replay stall is 0
  cycles; back-to-back bad-CRC frames cost 1 cycle each. The design
  accepts 1 byte/cycle against a line rate of 160 clocks/byte at
  `CLK_PER_BIT` = 16 (80 at the proposed 8); the TB drives 1 byte per
  2 clocks (~40-80x line rate) as a stress case. Overrun — a byte
  arriving while rescan is still pending — is structurally impossible:
  there is no multi-cycle rescan state. Pinned by a back-to-back
  bad-CRC-with-inner-SOF stream plus the two-A5 directed test (below).
  Rationale: stated now so Week 5 `top.v` wiring needs no FIFO sizing
  argument later.
- Double-A5 resync: a bad-CRC candidate holding two `0xA5` bytes with a
  valid frame starting at the second one must emit exactly that frame
  after two `crc_err` pulses (first-SOF-wins, iterative rescan across two
  completions). Pinned in two geometries, (p1, p2) = (4, 8) and (1, 5),
  covering short and longest rescan shifts, byte-exact vs the golden both
  sims. Rationale: the (1, 5) geometry is the regression net for the
  rescan off-by-one mutant class.
- `framing_error` hole behavior DEFINED: a `uart_rx` framing_error
  (bad stop bit) deletes exactly one byte from the stream — `frame_rx`
  has no side channel for it and needs none. The straddling candidate
  CRC-fails, `crc_err` pulses, resync lands on the next SOF; the two
  frames straddling the hole are lost, neighbors survive, no latch-up, no
  spurious frame. Proven end to end through the test-only integration
  top `tb/uart_frame_int.v` (wires `uart_rx` -> `frame_rx`; never
  synthesized, never shipped) with expected frames from
  `parse_l0_stream` over the bytes `uart_rx` actually emitted.
  Rationale: the hole is a byte-stream property, so the byte-level proof
  is the whole proof; `top.v` reuses the same wiring.
- Health counters: `crc_err_cnt`/`resync_cnt`, 16-bit saturating (sticky
  at `0xFFFF`), cleared on reset. `crc_err_cnt++` on every CRC rejection;
  `resync_cnt++` iff the rejection reuses an inner-SOF prefix
  (`rescan_pos != 0`; back-to-hunt counts only in `crc_err_cnt`).
  Saturation proven naturally, not by writes: simulator VPI writes
  (deposit AND force) demonstrably do not land on these Verilator flops
  (measured: readback stays 0x0000), so 70,000 bad-CRC frames each
  carrying an inner A5 drive 141,063 rejects AND resyncs
  (reference-counted, ~2.15x margin over 0xFFFF), sticking both counters
  at 0xFFFF. Rationale: telemetry for drop monitoring, sized to the L0
  rate without pretending to be policy.
- L0 counter monotonicity is enforced ONLY in the Python receiver
  (`edge/receiver.py` strict-reject tracker; records in Week 5
  `ledger/verifier.py`). `frame_rx` outputs the counter field verbatim
  and holds no expected-counter state. Rationale: replay/reorder/drop
  policy needs history and (at record level) keys; the framer stays a
  stateless pipe and the counters above stay telemetry, never gates.

## Week 5b: divider/sqrt FSMs + finalization/overrun spec (done)

### Latency derivation (measured == 401)

Counting convention (used by every TB assert — read this first): edges
are counted from the `win_end` sampling edge E0. An event labeled E_k
happens AT clock edge k (registered outputs settle just after). Latency
= `out_valid` rising edge index − `win_end` sampling edge index + 1
(inclusive: E0..E400 = 401). Unit latency likewise: done edge − start
sampling edge + 1 (div 65+1 = 66, sqrt 33+1 = 34). Rule with no
exceptions: a result register may latch at earliest one edge AFTER its
producer's done edge (same-edge reads see pre-edge stale values — this
exact bug class was caught on the PF path during bringup).

| Edge | Event | Gap from prev start |
|---|---|---|
| E0 | `win_end` sampled: shadow latch (sums/M/valid), busy<=1 | — |
| E1 | DIV_PAVG start sampled (operands: shadow) | — |
| E2 | emult / p_avg_lut multiply regs latched (parallel hygiene) | — |
| E66 | DIV_PAVG done | — |
| E67 | latch p_avg_exact; DIV_ENERGY start sampled | 66 |
| E132 | done → latch energy | — |
| E133 | DIV_V2 start sampled | 66 |
| E198 | done → latch mean_v2 | — |
| E199 | DIV_I2 start sampled | 66 |
| E264 | done → latch mean_i2 | — |
| E265 | SQRT_V start sampled (x: mean_v2 holding) | 66 |
| E298 | done → latch vrms (saturate) | — |
| E300 | SQRT_I start sampled (x: mean_i2 holding) | 35 |
| E332 | done → latch irms | — |
| E334 | DIV_PF start sampled (num: pavg<<15, den: vrms×irms, forced 1 if 0) | 34 |
| E399 | DIV_PF done | — |
| E400 | latch pf/m_out/valid_out/p_sum; `out_valid` rises; busy<=0 | — |

Every window, valid or invalid, both sims, both rates. Sqrt ops are
deliberately sequenced, not overlapped with divs, for a trivially
verifiable linear schedule; utilization stays under 3% of the tightest
real-rate finalization budget (13,200 clocks), so overlapping would buy
nothing. (Correction history, kept honest: the first draft summed to 402
with wrong sqrt spacings; bringup then caught a same-edge stale read on
the PF path (E398 vs true done E399). The map above is normative.)

- Shared serial divider justification: tightest real-rate finalization
  budget is 13,200 clocks (11-sample pathological spacing × 1200;
  valid windows give 2,172,000) against 401 cycles — under 3% utilization.
  Separate units (≈5 dividers + 2 sqrt roots of area) buy zero functional
  need. Rationale: area for need, and one unit means one proof.
- Constant-latency rule: every window runs the full schedule; invalid and
  zero-denominator paths force safe operands and zero the outputs as
  today. Rationale: a single number to pin, no data-dependent timing.

### Per-window operand bounds (why 64-bit units have headroom)

- |power sums| ≤ 2230·32768·32767 = 2,394,371,194,880 < 2^42 (43-bit
  signed minimum per sub-window; record sums < 2^44, Week 4a3).
- Energy numerator |p_sum|·12500 ≤ 2.993e16 (≈3.0e16) < 2^56.
- PF numerator |p_avg|·32768 < 2^47 (p_avg fits i32).
- Sqrt input (half-away mean of squares) ≤ 32768² = 2^30, so root ≤
  32768 (the documented 32767 saturating corner); the sqrt root port is
  33-bit wide because `isqrt_round_half_up(2^64−1)` rounds to 2^32, which
  a 32-bit port would wrap to 0.
- The controller guarantees |numerator| < 2^56 on every divide path
  (div unit precondition: |N| < 2^63, den > 0).

### Spacing, busy, drop, tombstone

- Minimum spacing between finalization starts = **11 sample periods**:
  closing needs 11 detections (counts 0→10, the closing detection
  recounts as 1 of the next) and strobes fire valid or not; `gap ≥ 150`
  governs validity only. Valid windows space ≥ 1810 periods. Real rate:
  13,200 / 2,172,000 clocks vs 401 (33×/5400× margins). Stress rate
  (1 sample/cycle): 11 cycles → overrun by design on spurious streams;
  1810 on clean streams (4.5× margin, never overruns).
- `busy` is asserted at the shadow latch and cleared with `out_valid`; a
  `win_end` coincident with `out_valid` latches new (idle-tie rule). A
  `win_end` while busy is a **DROP**: no shadow update (the ws/sample_valid
  path still attributes the E sample to the next window exactly as in
  normal closes, so nothing contaminates anything); instead a
  **tombstone** `out_valid` fires 1 cycle later (all-zero data,
  `valid_out` = 0, `m_out` = 0) with a per-slot `w_dropped` = 1 marker;
  `finalize_overrun` (sticky) and `finalize_overrun_cnt` (saturating
  16-bit, telemetry) increment per drop. Tombstones never collide with
  data completions (deferred past the completion edge; drops space >= 11
  cycles apart, deferral <= 1, so no merge is possible — each drop yields
  exactly one tombstone, proven by TB drop/tomb counts matching).
- Invisibility argument: a counter-less drop leaves nothing — record
  boundaries shift and `window_start` jumps with no attribution, which is
  bit-identical to a ledger-deletion attack's observable. A
  tombstone-without-count is only marginally better (a drop is then
  indistinguishable from a natural invalid window, blinding overrun
  monitoring). Hence tombstone + count + flag together.
- Design correction (kept honest): the first design carried a running
  overrun snapshot per window for record_agg to take at close. The
  atomic-clear test proved no clear timing keeps that exact: record_agg
  samples one edge after power's edge (+1 handoff), so any clear covering
  the close wipes what the next snapshot needs, while deferring the clear
  strands in-flight tombstone evidence. Tomb-flag counting has no such
  hazard — the evidence rides atomic with its slot — so the snapshot
  scheme was replaced (power counter stays as telemetry only).
- Shadow latch captures `{sh_p, sh_v2, sh_i2 [63:0], sh_M [11:0],
  sh_valid}` from the pre-sample accumulator values at the `win_end`
  edge ([S, E) ownership preserved: E excluded from the old window,
  owned by the new one); the next window accumulates into `sum_*`
  concurrently with finalization.

### Record mapping + descriptor layout change (Week 3 table amended)

- `record_agg` counts tombstone slots (`w_dropped` = 1) per record into
  `rec_overrun_cnt` (3-bit, **0..5 BY CONSTRUCTION** — 5 slots, one drop
  each — week5c review amendment; the u8 saturation logic was REMOVED as
  unreachable, the u8 port is the descriptor byte, zero-extended) and sets
  `window_flags` bit 6 = OVERRUN iff the count > 0. A dropped sub-window therefore appears as
  an **invalid slot with `zc_samples` = 0 and its validity bit clear**
  (the existing `w_ok` = 0 path — no new slot logic), distinguished from
  a natural invalid solely by `w_dropped`/bit 6 + count. Record slots
  fill in ARRIVAL order, not window order (a 1-cycle tombstone lands
  before 401-cycle data): golden/model glue must use arrival order;
  savings math (sums) is order-free, and the hash covers slots in the
  same arrival order on both sides.
- `reserved` byte → `overrun_cnt` u8 (36-byte layout unchanged;
  non-overrun records still pack `0x00`, so all week3 vectors are
  unaffected). Rule: **count > 0 IFF bit 6 set** (enforced where built,
  verified where received).
- Gap definition (samples, lower bound):
  `gap = next_window_start − (prev_window_start + prev_attested_span)`;
  rule `gap ≥ overrun_cnt × 11` (11 = minimum dropped-window span), with
  count ≤ 5 exact by construction. The old "255 reads as ≥255" saturation
  rule is REMOVED as unreachable (week5c review amendment): no path
  produces a count above 5, so the bound never degrades to a sentinel.
- Worked example (tombstone gap): windows w1[0,2000), w2[2000,4000),
  w3 DROPPED over true span [4000,4200) (tombstone slot, M recorded 0),
  w4[4200,6200), w5[6200,8200). Record window_start = 0; attested span =
  2000+2000+0+2000+2000 = 8000 (valid-M sum, order-free); next record
  starts at 8200. Gap = 8200 − (0 + 8000) = 200 unattested samples;
  bound check 200 ≥ 1×11 holds. Without the count+flag, this 200-sample
  jump would be indistinguishable from a deleted window (tamper).
- Week 5d verifier obligations (stated, NOT built): gap == 0 required
  when bit 6 is clear (any unattributed jump = tamper); the bound
  checked when bit 6 is set; bit 6 without gap = tamper. Count and flag
  are descriptor bytes, hence covered by HMAC + Ed25519 signature.

### Counter consumption (replaces the 5a2 latch-delta rule)

- Clear-on-latch: after consuming a window's outputs, top.v/TB pulses
  the shared `cnt_clear` sync strobe; err/resync/overrun counters zero,
  `cnt_saturated`/`finalize_overrun` stickies clear on reset only. Two
  phases per close: consume, then clear. The record path never reads the
  power overrun counter (tomb flags carry the evidence), so its clear
  timing is unconstrained telemetry hygiene — no atomicity hazard.
  Rationale: exact attribution with no subtraction logic; a skipped
  boundary merges counts (documented degradation, same as delta — but
  never loses them silently).
- `framing_error` counter: `uart_rx.framing_err_cnt[15:0]` saturating +
  `cnt_clear` + `FERR_CNT_INIT` parameter (default 0; test-only overrides
  via cocotb-test `parameters=`; top.v and the Yosys run MUST use the
  default — checked at those steps). `framing_error` still has NO path
  into `frame_rx`.
- Same-cycle rule everywhere: **increment wins** (clear zeroes, an error
  in the same cycle counts from zero → 1).
- Overrun one-liners: rescan replay stall is 0 cycles against an 80-clock
  byte period, so byte-level overrun is structurally impossible;
  finalization overrun is by design at stress rates and impossible at
  real rate (13,200-clock minimum spacing vs 401 cycles).

### Unit contracts

- `div_fsm(neg, mag[63:0], den[63:0] > 0)` bit-equals
  `div_round_half_away` (|N| < 2^63 precondition, sign-magnitude +
  half-away `2·rem ≥ den → +1` then negate).
- `sqrt_fsm(x[63:0])` bit-equals `isqrt_round_half_up`, root out
  `[32:0]`, round-up iff `rem > root`.

## Week 5c: secworks SHA-256 + HMAC (review-amended plan)

- C0 provenance (cloned, then nested `.git` removed so the tree is
  self-contained): `https://github.com/secworks/sha256`,
  commit `837c5cc396f001d18f2c765721c585716eb439ae` (2025-12-15, merge
  PR #26), into `rtl/third_party/secworks-sha256/`. License: BSD
  2-Clause (LICENSE file, © 2013 Joachim Strömbergson; RTL headers add
  Secworks Sweden AB) — recorded in `docs/THIRD_PARTY.md`. RULE: the
  core is never modified; our wrapper lives OUTSIDE `third_party/`.
- Instantiation target is `sha256_core` (wide interface, NOT the
  memory-mapped `sha256.v` top): ports `clk`, `reset_n` (active-LOW
  async — the wrapper inverts our sync active-high `rst`; assert/deassert
  behavior noted at build), `init`/`next` (sample the 512-bit `block`
  input; hold it stable until the core retakes `ready`), `mode` (tie 1
  = SHA-256; SHA-224 support NOT wired), `block[511:0]`,
  `ready`/`digest[255:0]`/`digest_valid`.
- Core latency 66 cycles/block (secworks README FPGA results across
  Cyclone/Spartan/Artix/Zynq + `SHA256_ROUNDS = 63` + IDLE/ROUND/DONE
  states; TB-MEASURED ± figure replaces this citation at build). The
  core does NOT pad (README: "caller handles padding") — the wrapper
  owns 0x80/length encoding, which is why the pad-boundary vectors
  (below) exist.
- ONE core, time-multiplexed (week5c review amendment): the HMAC input
  IS the chain output (`HMAC(key, descriptor || window_hash)`), so the
  two phases are data-dependent and no second core buys parallelism
  within a window; across windows there is nothing to gain either (one
  window hashes in ~6.9 ms against a 1 s record period). Schedule:
  PHASE_CHAIN streams `prev_hash → samples → descriptor` (the reorder
  above), latches the 32-byte digest as WINDOW_HASH, then PHASE_HMAC
  runs the key schedule in the wrapper (`key⊕ipad`/`key⊕opad` blocks;
  keys > 64 bytes are pre-hashed per FIPS) + inner + outer passes
  through the SAME core. Refinement (build): `hmac_core.v` holds NO
  engine — it sequences an EXTERNAL `sha256_wrap` (ports below), so the
  single core is literally shared; `tb/hmac_wrap_pair.v` wires the pair
  for unit tests and top.v (5d) will own the mux. The HMAC message is a
  streamed byte string of ANY length (not a fixed 68 B port): the
  attestation path streams `descriptor || window_hash` (68 B) through
  it in 5d. Latency table (core-cycles; HMAC rows are formulas with the
  68 B attestation reference; wrapper control MEASURED at build, table
  updated with ± figures):
  | phase | message bytes | blocks | ×66 |
  |---|---|---|---|
  | chain | 32 + 80,000 + 36 = 80,068 | 1,252 (1,251 full + 1 pad-carrying) | 82,632 |
  | hmac-inner | 64 + L | ceil((64 + L + 9)/64) (= 3 at L = 68) | measured |
  | hmac-outer | 64 + 32 = 96 | 2 | 132 |
  | hmac total (L = 68) | — | 5 | 335 + control = 473 (measured, week5c3) |
- Backpressure is a REAL signal (week5c review amendment): `ready` =
  input FIFO not-full, `FIFO_DEPTH = 256` bytes (4 blocks — absorbs a
  full padding flush plus TB burst phasing; tiny). The stress producer
  HONORS it (pauses; run completes exact, overflow flag stays clear). A
  producer that IGNORES it (test-only misbehaving source) sets a STICKY
  `overflow` flag — never a silent drop. Throughputs: hash input is
  8 B/sample × 10 kHz = **80 kB/s** (the 110 kB/s figure is the UART
  line rate incl. SOF+CRC, not the hash input); core delivers
  64 B / 66 cyc × 12 MHz ≈ 11.6 MB/s, margin ~145×, so `ready` never
  drops at line rate (asserted in test). Rate labels (week5c4
  correction — values unchanged, the old "1 B/120 cyc ≈ 80 kB/s" label
  was wrong arithmetic): the TRUE hash-input line rate is 80 kB/s =
  1 B per 150 FPGA cycles @12 MHz; the TB "line" pace (gap 119 =
  1 B/120 cyc = 100 kB/s @12 MHz) runs 1.25× HOTTER than reality, so
  the near-empty peak is conservative, not exact-rate. TB stress at
  1 B/cycle
  (12 MB/s) EXCEEDS the core rate BY DESIGN, so backpressure engages
  and the honoring/ignoring pair proves both paths.
- Build-phase test contract (week5c review amendment): NIST vectors
  (empty/abc/448/4480-bit/1M) + pad boundaries 55/56/63/64/119/120 +
  full-window 80,068-byte message vs `hashlib` + 8,191/8,192/8,193-byte
  lengths + back-to-back messages (init discipline) + RFC 4231 cases
  1–7 + 200 seeded random pairs; mutants: bit-vs-byte length,
  little-endian length, init skipped, FIFO overflow without flag
  (overflow path), key⊕ipad/opad swap; HMAC keys from
  `sim/provision.py` except the fixed RFC vectors. Wrapper tests carry
  `sim=` both-sims like Week 5b; the 80,068-byte case may earn a slow
  mark from its measured time (not assumed).
- HMAC key custody (week5c review amendment, threat-model entry): the
  key lives in an FPGA register at runtime; Week 5 proves key USE
  (bit-exact HMAC), NOT key PROTECTION — physical extraction is out of
  scope (see `docs/threat_model.md`).

### sha256_wrap measured behavior (Week 5c build)

- Contract (ports in `rtl/sha256_wrap.v`): `init_in` (hold ≥ 2 cycles)
  aborts anything and opens a message; bytes stream with `valid_in`
  honoring `ready_out` (FILL && FIFO not full); `last_in` WITH the
  final byte (or lone `last_in` for the empty message) seals; after the
  seal ready reads LOW and further valid bytes set the STICKY
  `overflow_out` (cleared by init/rst) while the in-flight digest stays
  exact; `digest_out` latches with a 1-cycle `digest_valid_out` pulse;
  `occupancy_out` (0..256) is the status tap for peak reports.
- Core facts discovered (not assumed): the secworks `digest_valid` is
  STICKY-HIGH until the next accept (not a pulse) — so the wrap counts
  digest EDGES (`dones`), and the TB does the same. Measured, both sims
  identical (pinned in `test_hash_backpressure`). Unified
  measured-vs-derived table (week5c3: every row carries BOTH columns):
  | quantity | derived | measured |
  |---|---|---|
  | core accept → digest_valid | 66 (secworks README) | 66 on all blocks |
  | digest_valid → next accept (re-arm) | ≤ 2 (FSM: see-ready, pulse) | 1 (spacing 67 on 78 spacings) |
  | block spacing (core-bound; assembler needs 64) | 66 + 1 re-arm = 67 | 67 |
  | final digest_valid → wrap digest_valid (drain) | 1 (registered completion) | 1 |
  | stress peak (5,000 B @ 1 B/cycle) | fills (net +3 B/block over 79) | 221, overflow clear, ready dropped |
  | line peak (TB "line" pace 1 B/120 cyc = 100 kB/s @12 MHz; true rate is 1 B/150 cyc) | near-empty | 1 |
  Reconciliation (week5c3): 67 vs 66 is NOT a discrepancy — 66 is the
  core accept→digest_valid cost, 67 = 66 + 1 re-arm cycle
  (digest_valid→next-accept, measured 1). Every block-to-block spacing
  and every HMAC core-cost formula below uses 67 per block on purpose.
- Abort-reset rationale (proven by mutant M-H6, not assumed): the wrap
  holds the core in reset while `init_in` is HIGH. Removing it breaks
  every multi-message run: the sticky digest_valid from the previous
  message is still HIGH when the re-init clears the edge detector, so a
  PHANTOM digest edge pre-counts one block — the next message then
  completes one block early latching an intermediate digest (traced:
  outer pass latched block-0's digest as the "tag"). Single-message
  runs are unaffected (5 nodes survived M-H6; all 12 multi-message
  nodes failed). The reset term is therefore load-bearing for every
  message after the first, not just mid-stream aborts.
- Sticky-digest_valid invariant + directed test (week5c2 item c):
  `test_hash_abort_restart` aborts mid-FILL (100 bytes, ≥ 1 block
  handed, core hashing) and inside the sticky window (one block done,
  digest flag HIGH-sticky, next not accepted), then restarts: both
  restarts complete exact with `dones == accepts` (TB reads the DUT
  counters: 4 and 2) and exactly one new wrap digest each, no overflow.
  The sticky-window half polls the sticky flag (persists: race-free).
  The test FAILS under M-H6, so it is a second abort-path catcher
  alongside the back-to-back abort half — the invariant is pinned, not
  just the mechanism's absence.

### Item 1: tombstoned samples ARE hashed (Week 5c decision)

- The hash engine taps the validated L0 sample bytes as they enter the
  fabric (post-`frame_rx`), and is DROP-OBLIVIOUS: samples of a
  sub-window later tombstoned by `power_calc` are hashed exactly like
  any other wire bytes (the drop is a processing event, not a hashing
  event). Rationale: fixed message length for full windows (simpler
  verifier length check), and drops cannot hide sample tampering — the
  tombstone count+flag (descriptor path) says "unprocessed" while the
  hash still commits to the bytes.
- Total length derivation: `32 + 8 × N_acc + 36`, where `N_acc` =
  L0 frames accepted by `frame_rx` within the window span (CRC-bad
  frames never enter). Full window, no frame drops: `32 + 80,000 + 36
  = 80,068`. A tombstone changes NOTHING about the length (same
  `N_acc`); Python-side no code change was needed (`build_window_hash`
  already hashes the passed arrays; the tomb slot keeps its as-sent
  arrays with `valid=False`, exactly like a natural invalid).
- FPGA-side note (5d wiring): the tap needs `P_inst = V × I` bytes
  (16×16→32 exact combinational multiplier at the tap); `V`/`I` bytes
  come straight off the frame fields.
- Test: `test_hash_tomb` feeds a tombstoned-slot record's exact Python
  byte string through the wrap (== `window_hash_hex`), and asserts a
  clean record of the same shape hashes the SAME length.

### Item 5: verifier capability matrix (Week 5c analysis)

- Without the SAMPLES (record + keys only): the verifier checks the
  Ed25519 signature (pubkey), descriptor self-consistency (rebuild from
  fields vs transported bytes), counter monotonicity + chain linkage
  (`prev_hash` equality across records), the OVERRUN IFF + range rules,
  and recomputes the HMAC (needs the KEY, not the samples). It CANNOT
  recompute `WINDOW_HASH` or re-derive P_AVG/energy. Threat consequence:
  a sampleless key-holder detects tampering with descriptor/hash/sig/
  counters, but takes hash-correctness on the key-holder's behalf —
  except a forged hash still needs a forged SIG (SE key), so the
  residual risk is a key-holder miscomputing (not forging) hashes.
- Without the HMAC KEY (public third-party verifier, pubkey only): it
  checks signature, linkage, counters, descriptor syntax — everything
  except HMAC and hash recomputation. It trusts the key-holder for
  HMAC/hash correctness but independently verifies authorship (SIG),
  ordering (chain + counters) and descriptor integrity.
- With samples (L0 tap) but no key: adds hash recomputation (detects
  sample tampering incl. dropped-sample hiding); HMAC still needs the
  key. Full verification needs samples + key + pubkey (the Week 3
  `RecordReceiver` configuration); Week 5d's `verifier.py` will state
  which role it implements.

### hmac_core measured behavior (Week 5c build)

- `KEY_MAX = 160`: covers RFC 4231's longest key (131 B) with margin;
  longer is rejected (`error_out`, no `done`). The 160-byte parallel
  key port latches at `start` (zero-padded past `key_len`); the message
  streams (any length, incl. empty via lone-`msg_last`).
- Errors (sticky to start/rst, abort with NO done, busy drops):
  oversize key; wrap overflow outside `P_INIT` (`P_INIT` is masked:
  wrap-init clears a stale flag, so the first two cycles cannot judge);
  `start_in` while busy is IGNORED (top-level protocol guarantees
  IDLE-start; the TB asserts it).
- Measured job cycles (start-stream to `done`, both sims identical,
  pinned per vector in `test_hmac_rfc17`). Unified measured-vs-derived
  table (week5c3: every row carries BOTH columns; core cost uses 67 =
  66 + 1 re-arm per the wrap reconciliation above):
  | vector | key/msg bytes | blocks [pre, inner, outer] | derived (blocks × 67 + stream + control) | measured (job cycles) |
  |---|---|---|---|---|
  | c1–c5 | ≤25 / ≤50 | [2, 2] (4) | 4 × 67 = 268 + 135 = 403 | 403 |
  | c6 | 131 / 54 | [3, 2, 2] (7) | 7 × 67 = 469 + 204 = 673 | 673 |
  | c7 | 131 / 152 | [3, 4, 2] (9) | 9 × 67 = 603 + 204 = 807 | 807 |
  | attestation (68 B: descriptor 36 + hash 32, short key) | 32 / 68 | [3, 2] (5, no pre-hash) | 5 × 67 = 335 + 138 = 473 | 473 (busy_out, pinned in `test_hmac_overlap` + `test_hmac_skid`) |
  | HMAC window (same 68 B job, withhold/busy view) | — | 5 | 5 × 67 + control = 473 | 473 cycles, peak occupancy 86 (pinned) |
  Derived model: blocks × 67 (core-bound, 66 + 1 re-arm) plus stream
  bytes plus per-pass control (P_INIT holds, digest sampling, pass
  switches); residuals (job − 67 × blocks: 135 for c1–c5, 204 for
  c6/c7, 138 for the 68 B attestation job) are deterministic (TB paces
  1 B/cycle) and the exact pins guard any timing change in
  hmac_core/sha256_wrap. Restatement (week5c3): the attestation HMAC
  total is 5 × 67 + control overhead = 335 + 138 = 473 cycles.
  The 138 decomposed (week5c4, MEASURED one-off instrumented run,
  verilator, 68 B short-key job, tag exact vs oracle; the 473 total
  itself stays pinned both sims in `test_hmac_overlap` /
  `test_hmac_skid`). Convention: busy-poll edges (the skid pin counts
  edges sampled busy-high; the start-sample and done/IDLE edges fall
  outside, hence 473 not 474). Phase buckets, derived vs measured:
  | bucket | derived (structural) | measured [edges] |
  |---|---|---|
  | P_INIT holds + FINISH (sequencer control) | 1 + 2 + 1 (hold-2 design, poll convention drops the entry edge) | 1 + 2 + 1 = 4 |
  | stream phases IN_PAD / IN_MSG / OUT_PAD / OUT_MSG (TB 1 B/cyc) | 64 / 68 / 64 / 32 (byte counts) | 64 / 68 / 64 / 32 = 228 |
  | IN_DV wait (inner tail) | 66 dv + drain/sample + pass-entry | 136 |
  | OUT_DV wait (outer tail) | 66 dv + drain/sample + finish-entry | 105 |
  | total | 335 + 138 | 473 (4 + 228 + 136 + 105; exact sum) |
  Accept gaps (block spacing, measured): 67, 67, 136, 67 — three pure
  core-bound 67s (TB streams ≥ core drain, zero starvation) and one
  structural inter-pass gap 136 = inner-tail 68 (dv 66 + drain 1 +
  pass entry 1, events +202→+270) + INIT2 2 (+270→+272) + OUT_PAD 64
  (+272→+336) + outer handoff 2 (+336→+338). No gap exceeds its
  structural value by even 1 cycle on either sim.
- HMAC-window reconciliation (week5c3): CURRENT is 473 cycles (peak
  FIFO 86) — measured at the hmac level on the real 68 B attestation
  shape (`desc || digest`), both sims identical, pinned in
  `test_hmac_overlap` (`withhold_cycles == 473`, `peak == 86`) and in
  `test_hmac_skid` (`busy == 473` both jobs). The "~350 cycles" remark
  in `tb/w5_hash_cocotb.py::test_hash_overlap` was an early CORE-ONLY
  derivation (5 × 67 = 335 ≈ 350 with rounding, before stream pacing +
  per-pass control were measured) at the wrap level, where the TB
  withholds N+1 for a flat 400 idle cycles as a stand-in. It is
  SUPERSEDED: 473 − 335 = 138 cycles of stream + control were missing
  from the estimate. The wrap-level 400-cycle withhold still passes
  (it only proves withhold-correctness, not the window length), but it
  does NOT pin the window — the hmac-level 473 pin is normative. The
  skid derivation below cites 473 (correct). The stale "~350" comment
  is updated to point at 473 in this commit.
- Sharing proof (item 2, hmac level): `test_hmac_overlap` chains N
  (2,000 B) → HMAC over `descriptor || N-digest` → chains N+1
  (1,000 B) through the ONE core with N+1 withheld during the HMAC
  window: all digests exact, no overflow/error, HMAC window 473 cycles
  (pinned), peak occupancy 86 (pinned). 5d skid obligation (week5c2
  item f, derived — replaces the rough ~3.2 B estimate; clarified
  week5c3): samples are ATOMIC 8-byte units at 1 per 1200 FPGA cycles
  (10 kHz @ 12 MHz), so a 473-cycle HMAC window at LINE RATE contains
  at most ONE sample tick = 8 B realistic (473 < 1200, so two ticks
  can never both land inside; the realistic bound is 1 sample, with
  2 samples = 16 B as the absolute margin case). What the old
  "~10 B realistic" counted was the TEST schedule, not line rate: job
  1 of `test_hmac_skid` queues an 8 B sample burst PLUS 3 single-byte
  line ticks at busy-cycles 150/300/450 = 11 B in-window (rounded to
  "~10" in the week5c2 note), strictly MORE pressure than the 8 B
  line-rate reality — the proof is conservative by construction.
  Depth 16 is therefore the tight power-of-2 bound: 2× over the 8 B
  line-rate realistic case, exactly covers the 11 B test burst + the
  16 B back-to-back margin job, covers a future doubled window, and
  matches FIFO addressing. `test_hmac_skid` models the skid
  as a queue with the bound ASSERTED (not enforced): greedy source
  (append-while-room, strictly more pressure than line rate), job 1 =
  physical worst case (8 B sample burst + line ticks at 150/300/450,
  window pinned 473), job 2 = margin case (back-to-back 16, peak pinned
  == 16 — the bound is load-bearing, never vacuous); drain order is
  checked by digest (burst8+line3 concatenation, burst16). A 16-deep
  skid in the 5d chain feed covers all of it with margin (top.v scope).
  Overflow beyond 16 is NOT silently droppable: `skid_put` asserts
  (fail-loud in simulation) and the wrap-level `overflow_out` flag +
  hmac `error_out` abort path (pinned by `test_hash_overflow`,
  `test_hash_sealed_force`, `test_hmac_overflow_abort`) proves the
  hardware equivalent signals instead of dropping — see
  `tests/test_week5c_skid_overflow.py` (17-deep: 16 queue fine, 17th
  raises; the 16 drain in order, digest-checked).

### Week 5c4: harness hardening (dataclass sweep + crash forensics)

- Frozen-dataclass sweep (week5c3 fixed `tb/golden.py` after the
  `__dataclass_builtins_object__` NameError; week5c4 greps
  `tb/ sim/ edge/ ledger/` for the same mechanism — frozen
  `@dataclass` exec's an `__init__` closed over a builtins cell, the
  flaky path; non-frozen uses plain `self.x =`, unaffected):
  | class | in-sim construction | disposition |
  |---|---|---|
  | `sim/provision.KeyMaterial` | YES, hot (200×/run in `random200`) | converted → NamedTuple |
  | `edge/framing.Frame` | YES (`decode_frame` in frame/fuzz TBs) | converted → NamedTuple |
  | `sensors/windows.ZCParams` | YES, once per sim at TB import (+ `@property`s, preserved) | converted → NamedTuple |
  | ADC/AFE/Profile/Motor/Thermal params, host `ZCParams` uses | NO (once per host run) | LEFT: zero failures in 400+ host passes; churn without evidence |
  | all non-frozen dataclasses | n/a (no builtins closure) | LEFT (mechanism-specific) |
  All three conversions are drop-in (same attributes/defaults/
  equality/hash); no `replace/asdict` users exist (only
  `run_week2.py` reads `__dataclass_fields__` on unconverted
  AFEParams). Smoke: `random200` + `fuzz_corrupt` + 30 host tests green.
- `test_hash_1M` third-flake forensics: the 1M path constructs NO
  dataclasses, so the NameError mechanism is excluded. Captured failure
  (campaign iter 23) is a SIM-PROCESS DEATH — `terminated with error
  -11`, three consecutive (first attempt + retry + waves rerun), zero
  assertion content. macOS crash reports pin it: EXC_BAD_ACCESS /
  SIGSEGV with the faulting thread inside CPython IMPORT machinery
  (`PyImport_ImportModuleLevelObject` ← `builtin___import__` ← exec),
  ~170 ms after process launch (startup imports, before the test body).
  Verdict: INFRA class (same family as the week5b2-tracked SIGSEGVs),
  clustered by transient host state — NOT the DUT (no verdict rendered;
  same binary passes before/after). Retry policy behaved as designed
  (no-verdict crash retried; double-crash still fails hard, counted
  FLAKY, never green). Observed 1M-verilator rate ≈ 1/300 node-runs
  (heaviest VPI test) vs the ~0.1%/node background.
- gtkwave: `brew install gtkwave` is UNINSTALLABLE (formula
  deprecated/disabled 2025-10-29, discontinued upstream). No local FST
  inspector exists; forensics rest on captured tracebacks, macOS crash
  reports, and file-written measurement tables (prints vanish in-sim —
  established week5c4 when the instrumented probe's `print`s never
  surfaced; `results/meas_*.txt` is the durable channel).

### Week 5d2: blocking answers 1–3 (pre-RTL — no top.v code yet)

### B1. Production UART: CLK_PER_BIT = 8 at 12 MHz / 1.5 Mbaud

- Value: 12 MHz / 1.5 Mbaud = **8 clocks/bit**; `top.v` instantiates
  `uart_rx #(.CLK_PER_BIT(8))` and all e2e tests run at it (nominal).
- Sampling adaptation: NONE structural — the sampler was never "16x
  oversampling"; it is a mid-bit-centered single-sample-per-bit design
  fully parameterized by `CLK_PER_BIT` (mid-start check at `HALF_BIT`,
  data/stop samples every `FULL_BIT`). What did NOT fit was width
  hygiene under `-G` elaboration (the exact path production uses):
  `HALF_BIT = CLK_PER_BIT >> 1` tripped WIDTHTRUNC-as-error at
  `-GCLK_PER_BIT=8`, fixed with explicitly-sized selects
  (`{1'b0, CLK_PER_BIT[11:1]}`, `CLK_PER_BIT[11:0] - 12'd1`); the stale
  "16x" header comment corrected. Re-verified: `test_week4b_uart`
  (both sims, CLK_PER_BIT=16) still green after the edit.
- Re-measured edges at 8, BOTH sims identical (temp probe, 4 frames per
  vector, deleted after): **−2.5 % pass / −2.6 % fail; +7.5 % pass /
  +7.6 % fail** (100 %-pass edge definition). The asymmetry flips vs
  16 (−3.5/+5): the tight side is now negative.
- Production tolerance decision: **±1 %** (1.6 pp margin on the tight −
  side — same ≥1.5 pp rule that set ±2 % at 16 in week4b2). The week4b
  ±2 %@16 pins stand as module characterization; e2e runs at nominal.
  A permanent CLK_PER_BIT=8 unit node was deliberately NOT added (the
  5d2 e2e at nominal-8 runs every gate as functional coverage at the
  production setting; tolerance edges are recorded characterization).

### B2. HMAC key interface: write-once provisioning port (adopted)

- `top.v` ports: `hmac_key_data_in[255:0]` ([7:0] = key byte 0,
  LSB-first, same convention as `hmac_core`) + `hmac_key_valid_in`
  (latch pulse). Semantics: honored ONLY when idle AND unprovisioned;
  later pulses ignored; sticky `hmac_provisioned_out` for TB assert.
  The TB loads it from `sim/provision.py` `derive_keys()` before the
  first record (protocol precondition, asserted in test).
- `key_len` tied 32 internally (provisioned keys are 32 B; 64–160 B
  paths stay unit-covered at `hmac_core`).
- Fail-loud interlock: a record closing while unprovisioned raises
  sticky `error_out` with NO record (a zero key would mint
  valid-looking tags — silent is worse than loud).
- Yosys counts are key-INDEPENDENT by construction (256 key FFs behind
  an input port; values never specialize cells — 5d4 states the counts
  with the port undriven).
- Threat model (already `docs/threat_model.md` HMAC-key line, quoted):
  key in an FPGA register at runtime; Week 5 proves key USE
  (bit-exact HMAC under the test vectors), NOT key PROTECTION.

### B3. Repo-check + counter-test rules (spec; implemented in the 5d2 build)

- Static check (`tests/test_top_no_test_params.py`): FAILS on any
  `FERR_CNT_INIT|OVERRUN_CNT_INIT|CNT_INIT` token in `rtl/top.v`; on
  any `#(...)` override in `top.v` instantiations EXCEPT the whitelist;
  on `-chparam|-P |-G<param>` in `synth/*.ys`. Whitelist (closed):
  `LUT_MEAN_ENABLE(0)`, `DEVICE_ID(x)` (TB overrides via `parameters=`
  for device tests; Yosys default), `CLK_PER_BIT(8)` (production value
  per B1 — not a test hook). Declarations with default 0 are allowed
  as a general rule; `top.v` declares NONE of the test hooks (the
  counter runs default 0 in production/top/synth, always).
- Elaborated-parameter dump check (5d4 implements): synth flow emits
  `write_json`, a python step asserts the top cell's parameters
  (`LUT_MEAN_ENABLE=0`, `DEVICE_ID`=default, no test-hook overrides,
  `CLK_PER_BIT=8`).
- Halt-at-max: unit-tested via `parameters=` on the direct
  `win_counter` wrapper (EXISTS: `test_counter_halt`, CNT_INIT=MAX−2).
  Through top: e2e asserts the counter increments per record (count
   path proven end-to-end) and `halted_out == 0` in normal operation
   (port-wiring proof); untestable-to-1 honestly stated (2^32 closes
   unreachable — the halt LOGIC is the unit pin).

## Week 5d2: top.v integration (built — commit, then stop for review)

- Scope: `rtl/top.v` (909 lines) wires every 5d1 unit end to end
  (uart → frame → zc/power → record_agg → tap/skid → chain-wrap mux →
  HMAC → publish), plus new `rtl/skid_buf.v` (16-deep FWFT, sticky
  overflow, drop-and-flag), `tb/w5d_top_cocotb.py` (12 cases),
  `tb/test_week5d_top.py` (both-sims runner), `tests/test_top_no_test_params.py`
  (B3 repo guard). Single-phase only; three-phase untouched.
- Sequencer phases `T_IDLE/ACCUM/DIVWAIT/ASSEMBLE/CHAINWAIT/HMACWAIT/HMAC/OUT`
  (`tap_phase` live). Mux rule: `wrap_sel_q` changes ONLY at job
  boundaries (chain-done → HMAC, HMAC-done → chain); per-master gated
  ready/dv/ovf. Chain stalls mid-HMAC are structural (back-to-back test).
- Boundary E-split (amendment 4): 5th `win_end` holds E's 8 B in skid for
  the NEXT job (`boundary_hold_q`, `window_start_next_q`); `SAMP_END`
  via lone `chain_samp_last_q`. Record k hashes
  `[E_{5k-1}, E_{5k+4})` (genesis `[0, E_4)`); exclusion proven by exact
  digests on BOTH sides (`test_top_boundary_E`).
- Sidecar (amendment 7): unsigned NOT-ATTESTED telemetry, latched at
  close, cleared one edge later (`cnt_clear`); saturated counters read as
  lower bound; carries `sc_counter`. Skid policy (amendment 5): 17th byte
  dropped + sticky bit7 `HASH_OVERFLOW` + invalidate (valid bits cleared,
  p_avg 0, energy excluded), counted.
- Provisioning (B2): FIRST-pulse-wins write-once; unprovisioned close →
  sticky `error_out`, no record; recovery is reset + provision + resend
  (`test_top_key_provision`, all three legs green both sims).
- Build findings (all fixed, all green below):
  - Same-edge start collision: a chain start firing the same edge as an
    HMAC start landed the chain's 2-cycle INIT inside the HMAC window
    (mux drops it) — chain wedged in PREV with no re-INIT while the HMAC
    completed normally (multi-record pending at `T_HMACWAIT`). Fix:
    phase-gated chain start (`T_ACCUM`/`T_IDLE` only, amendment-6
    hardening, `top.v:715`); post-`T_OUT` starts keep the fast shape.
  - Valid/data coherence: combinational `ser_byte` delivered byte[idx+1]
    on the push edge; fixed with registered `push_data_q`/`tap_byte_q`
    (8167 B model match). `wstart` staging (`desc_wstart_q`) and
    `first_start_done` spanning all records (an earlier `first_record_q`
    gate deadlocked post-close in IDLE).
  - Stale-tree debugging note: mid-build counter/sidecar failures with the
    pre-gate tree (`T_HMACWAIT` + chain PREV) reproduced only on builds
    predating the phase gate; the current tree passes the identical flows
    (`test_top_counter_path` green, change-only event log confirms
    need/hold/cnt/SAMP/DV progression on all 3 records).
  - TB mapping fix (DUT correct): `test_top_sidecar` phase-A tail
    (sent 1000+, bad at 1025..1375) lands in record 1's span, so r1 read
    `(8,0,0)` — the `(0,0,0)` expectation was wrong, not the sidecar.
    Fix: dirty frames confined to `< 1000` (all in record 0's span);
    r0 `(20,0,0)` → r1 `(0,0,0)` now proves latch+clear.
  - Repo-guard regex fixes (both caught before any gate; negative
    controls re-prove the catchers): `DEVICE_ID` default pattern matched
    past the declaration (now `[^;,()\s]+`); the override span swallowed
    the module header's `#(` and misattributed uart_rx's block to `top`
    (header stripped through first `);` first).
- Timing/span table, budget vs TB-measured (Verilator 5.048 + Icarus 13.0;
  `meas_open` truncates, so counter-path numbers are rec2):

  | # | quantity | budget/derived | measured |
  |---|---|---|---|
  | D-T1 | post-close `rec_done→valid` | ≤ 800-edge budget (~67 µs @12 MHz) | 644 (chunked 500 Hz) / 718 (full-shape 50 Hz), identical both sims |
  | D-T2 | `winend5→valid` | D-T1 + 402 (= 401 power + 1 stamp) | 1046 / 1120, identical both sims (1046−644 = 402 ✓, 1120−718 = 402 ✓) |
  | D-T3 | test bound | `POST_CLOSE_WAIT=20000` | ≥17× worst measured (20000/1120) |
  | D-T4 | inter-window gap | `POST_CHUNK_WAIT=600` (> E+401 close) | closes fire in-chunk every window |
  | D-T5 | skid peak, wire-rate record | < 16 (8 B/frame vs 880-edge spacing) | 8, both sims, no flag (`test_top_skid_bound`, e2e_small, device7) |
  | D-T6 | counter path | 0,1,2 + wstart continuity | exact, both sims |
  | D-T7 | sidecar dirty→clean | (20,0,0) → (0,0,0) | exact, both sims |
  | D-T8 | E-split spans | `[0,E_4)` / `[E_4,E_9)` digests | exact both sides, both sims |
  | D-T9 | P-tap bit-exactness | V\|\|I\|\|P LE bytes incl −32768² | corners + 40 seeded randoms exact |
  | D-T10 | key legs | ignore-2nd / error-no-record / recovery | all green both sims |

- Mutation table, fail scope + first catcher (each reverted;
  `grep MUTANT` clean; survivors: none):

  | # | mutant | first catcher |
  |---|---|---|
  | M-T1 | P off-by-one (+1; first attempt V+I died at compile WIDTHEXPAND-as-error — compile-kills don't count, redone width-clean) | test_top_pinst_exact / verilator (chunk 0 tap bytes) |
  | M-T2 | sidecar latch zero (crc) | test_top_sidecar / verilator (r0 (0,…) vs 20) |
  | M-T3 | 0xEE silent drop (no flag) | test_skid_overflow17 / verilator (17th byte no flag) |
  | M-T4 | LUT_MEAN_ENABLE(1) | test_top_instantiation_overrides_whitelisted (static; re-proven after the regex fix) |
  | M-T5 | mux switches mid-chain-job | test_top_e2e_small / verilator |
  | M-T6 | E not held | test_top_boundary_E / verilator (wstart (0,100021) on stale record — job wedges, nothing publishes) |
  | M-T7 | rejected frame latched + valid | test_frame_bad_crc / both sims |
  | M-T8 | skid full one early (15-deep) | test_skid_order / verilator (occ 15 vs 16) |

- Gate at build: `tb/test_week5d_top.py` + `tb/test_week5d_chain.py`
  40 passed both sims (13:30); repo guard 4 passed; `grep MUTANT` clean.
- Non-goals carried forward: NV counter (reset is volatile — HW needs
  one, verifier flags second genesis); key PROTECTION (register at
  runtime, out of scope per threat model); 2^32 halt e2e (logic pinned
  at unit); synth elaborated-parameter dump (5d4); three-phase.

## Week 5d1b: third-party startup death + retry-policy extension

- Our frozen-dataclass guard is necessary but NOT sufficient:
  `cocotb/__init__.py` itself does `import pytest`
  (`_decorators.py:22`) in EVERY sim process at startup, pulling
  pytest's frozen dataclasses
  (`_pytest/fixtures.py::FixtureFunctionMarker`, ...) into the
  interpreter path. A 5d1b gate run (`test_hash_overflow-verilator`)
  died exactly there — startup import, zero verdict, empty results XML
  surfacing as wrapper `ParseError`. Third-party code is not patched
  (unreproducible via `make repro`/CI), so the defense in depth is the
  retry policy: `is_infra_crash` now also matches the two no-verdict
  startup signatures (empty-results `ParseError`, the exact
  `__dataclass_builtins_object__` literal). A deterministic bug can
  never match either (genuine `NameError: name 'X' is not defined`
  stays False — pinned with negative controls in
  `tests/test_sim_retry.py` alongside the new True cases), so
  deterministic failures still fail twice and hard; only a clean rerun
  after a no-verdict event passes, loudly bannered. The failed node
  recovered green on immediate rerun, as predicted for a no-verdict
  event.

## Week 5d1: chain_ctrl + win_counter (done — commit, then stop for 5d2)

- `rtl/chain_ctrl.v` (new; sequencer, NO engine — drives one external
  `sha256_wrap`, same pattern as `hmac_core`; `tb/chain_wrap_pair.v`
  wires them for unit tests, `top.v` owns the mux in 5d2). Per record:
  latch prev (genesis zeros iff `prev_sel_genesis`, else the held
  digest) → stream 32 prev bytes → stream sample bytes (8 B atomic,
  ready-gated) → wait `desc_valid`, latch the 36 B descriptor → stream
  it (`w_last` on the 36th) → wait digest → latch `window_hash` (which
  becomes the next prev) → done. One wrap message per record:
  `prev || samples || descriptor` (week5c reorder). Empty tails seal
  NOTHING (`SAMP_END` moves on without `w_last`). `start` while busy
  IGNORED (hmac protocol). Errors sticky (abort, no done, busy drops):
  `sample_valid` while not ready; wrap overflow outside `INIT`
  (INIT-masked, same rationale as hmac `P_INIT`). Phases frozen:
  0 IDLE, 1 INIT, 2 PREV, 3 SAMP, 4 SAMP_END, 5 DESC_WAIT, 6 DESC_FEED,
  7 DV, 8 FINISH (pinned by TB `wait_phase` + transition asserts).
- `rtl/win_counter.v` (new, standalone): advances EXACTLY on valid
  closes (`incr_in && close_valid_in`); rejected closes (valid=0) never
  advance. HALT with STICKY flag at 2^32−1 — never wraps (wrap would
  reuse counter-derived nonces). Reset returns to `CNT_INIT`
  (volatile). `CNT_INIT` is TEST-ONLY (VPI writes don't land on
  Verilator flops); production/`top.v`/Yosys use default 0 (5d2 repo
  check). `CNT_MAX` is the spec constant, not a test hook.
- Measured-vs-derived (both sims identical, pinned in-test; week5d1b
  adds the derived column and total pins):
  | record | message bytes | blocks (derived) | derived total (blocks×67 + OH) | measured |
  |---|---|---|---|---|
  | genesis rec0 (24 samp) | 92 | 2 | 134 + 71 = 205 | 205 |
  | rec1 (40 samp) | 108 | 2 | 134 + 69 = 203 | 203 |
  | rec2 (16 samp) | 84 | 2 | 134 + 71 = 205 | 205 |
  | rejected-frame recB (181 acc) | 249 | 5 | 335 + 69 = 404 | 404 |
  | empty (0 samp) | 68 | 2 | 134 + 72 = 206 | 206 |
  | tomb (8000) | 8068 | 127 | 8509 + 69 = 8578 | 8578 (gaps 67 + total pinned) |
  Overhead rule (verified exact on all six shapes): OH = 69 base
  (INIT/PREV/first-fill/handoff + final tail beyond re-arm) + 2 iff
  block0 completes in DESC (descriptor handshake exposed instead of
  overlapped) + 1 iff empty (extra SAMP_END edge). The 203/205/206
  spread is therefore structural, not noise: 69 (block0 in SAMP:
  rec1, recB) vs 71 (block0 in DESC: rec0, rec2) vs 72 (empty:
  DESC + SAMP_END). SAMP length + DESC-to-64 always sum to 32 (bytes
  to fill block0 after PREV), so only the handshake/SAMP_END terms
  move the total.
  Structural pins (all records, both sims): every block spacing == 67
  (feed ≥ drain ⇒ the FIFO never empties mid-message at ANY length, so
  a stall gap is a BUG — pinned even on the 8068 B tomb); universal
  final tail last-accept → done == 69 (66 dv + drain + sample + done;
  the post-last-accept path is TB-free, hence length-independent).
  Totals are TB-convention (stream-start → done); rerun-identical on
  verilator, matched exactly on icarus.
- Named tests (5d approval condition 3 — all exist, cited here):
  halt-at-max with `CNT_INIT` near 2^32 + sticky flag =
  `test_counter_halt` (INIT MAX−2 → MAX−1 → MAX → halt on excess,
  stays MAX); rejected-frame digest == same record without that frame
  = `test_chain_rejected_frame` (record B over accepted-only bytes,
  plus `!=` full-bytes direction); "increment on rejected record"
  mutant M-C3 = `test_counter_mono` rejected-close cases (valid=0
  never advances).
- 473 phase breakdown is now a PERMANENT pin (5d approval condition
  2): `test_hmac_skid` asserts per 68 B job — 5 accepts with gaps
  [67, 67, 136, 67], 2 wrap-init rises (INNER + OUTER), 5 core digest
  rises (3 + 2) — the event-series view of the 9-bucket table
  (INIT 1 + IN_PAD 64 + IN_MSG 68 + IN_DV 136 + INIT2 2 + OUT_PAD 64 +
  OUT_MSG 32 + OUT_DV 105 + FINISH 1 = 473), both sims. Relative
  (gap/count) pins by design: robust to TB monitor phasing.
- Frozen-dataclass repo guard (5d approval condition 4):
  `tests/test_in_sim_no_frozen.py` walks the in-sim import closure
  syntactically (all `tb/*.py` entries, repo-local `tb/edge/sim/
  sensors` imports incl. function-level) and FAILS on any
  `@dataclass(frozen=True)` (plain, aliased, and module-attr forms —
  all three proven caught by probe, probe reverted). Fourth conversion
  under this guard: `sensors/adc.ADCParams` (import-time NameError in a
  5d1 sim startup — same mechanism, in-sim via `edge/framing`).
- 1M crash route (5d approval condition 5): the case STAYS OPEN.
  Waveform inspection is REPLACED as the crash route: VCD-on-failure
  reruns are KEPT (they preserve assertion-failure context), but crash
  forensics now rest on `PYTHONFAULTHANDLER=1` (set in all nine
  `tb/test_*.py` sim wrappers — a segfaulting sim prints a Python
  traceback instead of dying silent; retry policy unchanged) plus
  macOS crash reports. FST-viewer pursuit is DROPPED (gtkwave
  uninstallable). Environment on record: macOS 27.0 (26A428),
  Mac14,15 arm64; venv Python 3.12.13; sim log reports "Python 3.12.10
  interpreter at .venv/bin/python" (version-string skew vs the 3.12.13
  venv — recorded verbatim, open question, no mechanism claimed).
- Mutants (each reverted; `grep MUTANT` clean; survivors: none):
  | # | mutant | first catcher |
  |---|---|---|
  | M-C1 | field order (SAMP before PREV) | `test_chain_genesis_link` rec0 |
  | M-C2 | prev dropped (always zeros) | `test_chain_genesis_link` rec1 (rec0 genesis passes) |
  | M-C3 | valid gate removed (incr on rejected) | `test_counter_mono` rejected-close |
  | M-C4 | halt guard removed (wraps) | `test_counter_halt` (reads 0 past MAX) |
  | M-C5 | prev latch broken (stale prev) | `test_chain_genesis_link` rec1 (shared with M-C2) |
- Amendment decisions (5d plan amendments, decided here):
  1. Link-health counters (`crc_err`, `resync`, `framing_err`):
     UNSIGNED TELEMETRY SIDECAR (recommended, adopted) — `top.v`
     exposes them ALONGSIDE the record; `writer.py` logs them labeled
     NOT ATTESTED (outside HMAC/signature). REJECTED: descriptor
     extension (the 36 B layout is full; breaking it + all vectors for
     telemetry that needs no attestation is all cost, no binding gain).
     Decided before 5d2 as ordered (sidecar ports are 5d2 scope).
  2. Tail truncation: the ledger alone CANNOT distinguish truncation
     from a short-but-complete log — DOCUMENTED MISS without an anchor.
     `verifier.py` (5d3) takes an OPTIONAL anchor (expected last counter
     and/or head hash): with anchor → mismatch REJECTS; without → miss
     stated. Head-truncation test added to the 5d3 list.
  3. Counter policy: HALT + sticky at 2^32−1, never wrap/saturate
     (reuse = nonce reuse — forbidden). `CNT_INIT` near-max test pins
     it. Production hardware needs a NON-VOLATILE counter (this RTL
     resets volatile — stated, not hidden); the verifier (5d3) flags
     any second genesis or genesis-with-counter>0 as a reset event.
  4. Hashed samples: EXACTLY the in-window frame_rx-ACCEPTED bytes
     (post-CRC `V||I||P_inst`, 8 B atomic units). Out-of-window samples
     hash in their own windows; CRC-rejected L0 frames NEVER enter (no
     bytes on the wire). Pinned by `test_chain_rejected_frame` (11 B
     hole → digest over accepted-only; both directions: hole changes
     the digest, accepted-only verifies).
  5. Extra 5d3 tamper tests (distinct verdict per case): duplicated
     record, replayed old record, signature swapped between records,
     wrong public key, forged/second genesis, head truncation (with and
     without anchor — reject vs documented miss).
  6. Yosys (5d4): report LUT/FF/BRAM + LATCHES inferred (expect 0) +
     warnings; `nextpnr-ice40` is ABSENT locally (`brew install
     nextpnr` provides it — not installed per the amendment's else
     branch) so timing is UNCHECKED unless CI provides it; optional one
     vector through the synthesized netlist.
  7. Counter mutant "increments on rejected record" implemented as M-C3
     above (requires the `close_valid_in` gate in the design — the gate
     exists, is pinned, and production ties it per the D-02 rule that
     closed records (even invalid-flagged) advance; verifier-side
     rejection is 5d3 scope).
