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
  every figure below is pinned by an exact test assertion, not a side
  file — `meas_*.txt` are run residue only):

  | # | quantity | budget/derived | measured (= asserted, both sims) |
  |---|---|---|---|
  | D-T1 | post-close `rec_done→valid` | ≤ 800-edge budget (~67 µs @12 MHz) | counter rec0 617 / rec1 652 / rec2 644 / rec3 652; small 609; full 718 (`test_top_counter_path` EXPECTED, `run_e2e_small`, `test_top_e2e_full`) |
  | D-T2 | `winend5→valid` | D-T1 + 402 (= 401 power + 1 stamp) | 1019 / 1054 / 1046 / 1054; 1011; 1120 — same asserts (diff asserted == 402 on every record) |
  | D-T3 | test bound | `POST_CLOSE_WAIT=20000` | `>= 10x` max measured asserted (20000/718 ≈ 27x) |
  | D-T4 | inter-window gap | `POST_CHUNK_WAIT=600` (> E+401 close) | closes fire in-chunk every window |
  | D-T5 | skid peak, wire-rate record | < 16 (8 B/frame vs 880-edge spacing) | == 8 asserted, both sims (`test_top_skid_bound`, e2e_small, device7, e2e_full) |
  | D-T6 | counter path | 0,1,2,3 + wstart continuity | exact, both sims (4 back-to-back records) |
  | D-T7 | sidecar dirty→clean + counter | (20,0,0) → (0,0,0), sc[5]==k | exact, both sims |
  | D-T8 | E-split spans | `[0,E_4)` / `[E_4,E_9)` digests | exact both sides, both sims |
  | D-T9 | P-tap bit-exactness | V\|\|I\|\|P LE bytes incl −32768² | corners + 40 seeded randoms exact |
  | D-T10 | key legs | ignore-2nd / error-no-record / recovery | all green both sims |
- Decomposition of D-T1 (all terms exact-pinned, both sims; base =
  genesis-500Hz-valid rec0 = 617): validity −8 (small 609; m==0 path);
  chained-prev +27 (rec2 644 ≈ 617 + 32 B prev streaming − 5 edge
  overlap); arrival-phase jitter +0/+8 (rec1/rec3 652 vs rec2 644,
  deterministic per index — in-flight remainder at close varies with
  window-length parity); full-shape +101 (full 718 = 617 + 101).
  **5d4 sweep result (see 5d2c-O1 below): the +101 sits entirely in the
  CHAIN post-close busy tail** (`chain_tail` 534 invalid → 635 valid;
  `hmac_tail` 474 → 474 identical; `rec_done→valid == chain_tail + 75`
  exact on all 11 shapes, both sims). No stage mechanism is quoted
  beyond this localisation; item stays OPEN-but-localised, bounded and
  correctness-free.
- Yosys key-independence (B2 evidence, Yosys 0.66): `proc; opt; stat`
  over the full 5d2 tree with `hmac_key_data_in` tied 256'd0 vs all-1
  (temp wrapper top-with-output-ports, deleted after): **cell count
  3907 IDENTICAL** (re-measured on the 5d2c tree; prior commit-5d2b run
  measured 3899 on ITS tree — delta is the consume/drop-count logic).
  Wire totals are wrapper-sensitive and are NOT a metric. Values never
  specialize cells — counts are key-independent by measurement, not
  just construction. (LUT/FF techmap counts remain 5d4's job with the
  synth flow + elaborated-parameter dump.)
- Skid-overflow coverage (amendment 5; bit 7 `HASH_OVF`:
  `rtl/top.v:52` port doc, `:776-784` consumption — flags
  `(rec_flags & 0x60) | 0x80`, p_avg 0, energy held at previous
  cumulative, flag consumed at assembly):
  - Production path pinned at unit level (`test_skid_overflow17`:
    17th byte dropped AND flagged; M-T3/M-T8 catchers).
  - E2e trigger proven UNREACHABLE: harshest reachable flow
    (continuous, no inter-record idle — `test_top_continuous_clean`)
    runs r0/r1 both bit-exact with zero dropped bytes (sc[3]==0) and no
    flag; occupancy peaks ~15 against the 17 threshold (8 held + ~7
    arrivals over the ~780-edge job-start latency). Chunked flows peak
    == 8 asserted.
  - No e2e flag-flip exists, for the record: VPI deposits
    (`.value =` and `setimmediatevalue`) do not stick on this Verilator
    setup (landing readback stayed 0 after an edge advance) — a
    deposit-based test was written, failed at the landing assert, and
    was replaced by the continuous-flow pin above. Do not re-add a
    deposit test without re-proving deposits first.
- 5d3 verifier rule: REJECT any record with `window_flags` bit 7 set.
  Such records bind zeroed p_avg / energy-excluded fields and MUST NOT
  enter baseline or aggregation. The flag is per-record (consumed at
  assembly — never sticky across records); `sc_skid_ovf_cnt` carries
  the saturating drop-event count (lower bound) with `sc_counter`
  binding it to its record.

## Week 5d2c: SKID_DEPTH + e2e overflow proof + latch-up fix (review follow-ups)

- `SKID_DEPTH` (top.v parameter, default 16) is DEVICE_ID-class, not a
  counter hook: declared in top.v, passed as `.DEPTH(SKID_DEPTH)` to
  `skid_buf`, overridden in tests only via cocotb `parameters=`
  (the "wrapper" is the `test_top_skid_overflow_e2e` CASES entry with
  `{"SKID_DEPTH": 4}`, same mechanism as the DEVICE_ID=7 test — no
  wrapper file; production/CI/synth elaborate the default).
  Repo-guard policing (NOT `FORBIDDEN_TOKENS` — those are counter hooks
  that may never appear in top.v; SKID_DEPTH must appear, so it is
  policed instead): `DEPTH(SKID_DEPTH)` is the only whitelisted
  `skid_buf` override, `SKID_DEPTH` default pinned 16
  (`test_top_skid_depth_default_sane`), any other value fails
  (`test_top_instantiation_overrides_whitelisted`). Unsized literals
  throughout (`parameter DEPTH = 16`): `-GDEPTH=4` on a sized param
  tripped WIDTHTRUNC-as-error (found by the first depth-4 build).
- Latch-up found by the new e2e test (real bug, latent at depth 16):
  top re-latched its flag from the unit's sticky level EVERY edge while
  never clearing the unit, so after one drop the output port read 1
  forever, the count counted EDGES (→ saturate), and every later record
  flagged bit 7 with no new drops. Fix (no behavior change at depth 16
  — full suite green): consume handshake (`skid_consume_w`, 1 edge by
  construction at the assembly edge; drop-wins on coincidence) clears
  the unit flag; the unit counts exact dropped bytes
  (`drop_cnt_out`, clear-then-count on the shared `cnt_clear` strobe,
  amendment-7 discipline); top latches the unit count (its own
  edge-counting `skid_ovf_cnt_q` removed; tap-overrun keeps the flag,
  drops its unobservable count line). Unit-pinned: threshold follows
  DEPTH (`test_skid_depth4`: 4 in order, 5th drops+flags, wrap,
  consume, clear, drop-wins), count exact (`test_skid_overflow17` +1).
- E2e proof at depth 4 (`test_top_skid_overflow_e2e`, 50 Hz valid flow,
  2 records): burst-vs-drain races drop exactly one byte per record
  (both sims identical: flags 0x80, drops 1, peak 4): bit 7 set, valid
  bits cleared, p_avg 0 (computed nonzero — zc slots == golden M ≈
  2000, detection intact), energy 0 then unchanged (r1 == r0), sticky
  output observed raised pre-consumption and 0 after, sidecar count
  exact, tags bind the published fields (HMAC over actuals — drop holes
  sit inside the hashed spans, so no golden span is asserted).
- Peak bound (review item 2), corrected by measurement: NO flow gives
  ~15 — the ~15 in the 5d2b note was a TRIGGER ESTIMATE
  (`trigger computed reachable ~17.5`), never an attained peak, and it
  measured unreachable. Peak == 8 asserted in chunked single-record
  flows AND measured 8 in the harshest reachable flow (continuous
  2-record, `test_top_continuous_clean` asserts `skid_peak == 8`): the
  chain drain keeps up with bursts fully, so only E's held 8 B ever
  stand concurrently. Line rate (steady streaming, no boundary
  retention) stays <= 8.
- Open item 5d2c-O1 (review item 3) — **5d4 sweep: mod-64 hypothesis
  REFUTED, +101 localised to the chain tail; mechanism still OPEN.**
  The 5d4 `test_top_latency_sweep` (11 deterministic single-record
  genesis shapes: 8 fast ~500 Hz invalid-window ladders M_total 893–1204,
  3 slow ~50 Hz valid-window ladders M_total 9615–10416; each shape a
  fresh reset so every record is genesis, same base as the pinned 617;
  both sims bit-identical, results/meas_latency_sweep_{sim}.txt) measured:
  - The original hypothesis (post-close time steps by 67-cycle SHA blocks
    with total message length mod 64) is **refuted**: M_total varies 10x
    between families with ≤101 total spread, and within family the
    latency is NOT monotone in total-message-blocks (e.g. 152 blocks →
    534 cycles vs 113 blocks → 585 cycles).
  - The +101 valid-vs-invalid offset is real and reproducible, and lives
    **entirely in the chain's post-close busy span**: `hmac_tail` = 474
    on every shape (HMAC exonerated); `chain_tail` = 534 (invalid) →
    635 (valid); `rec_done→valid == chain_tail + 75` exact on all 11
    shapes both sims. Within family, `chain_tail` varies in ~8-cycle
    quanta (534,561,569,577,585 / 635,635,643) — the same arrival-phase
    residue family already pinned in 5d3.
  - No stage mechanism is quoted beyond "chain post-close tail, valid
    family" until instrumented inside chain_ctrl. Bounded (≤101 over the
    27x-margin test bound), identical both sims, no correctness impact.
    Cheapest next diagnostic: per-phase cycle counters inside
    chain_ctrl's PH_DESC_FEED / PH_DV path (guard-legal debug counters),
    or a same-M valid/invalid pair (not reachable with the current
    frequency/seed knobs, since validity is phase-determined).
  Figure clarification (unchanged): 644 is chained rec2 (`617 + 27`),
  NOT the small flow; the +101 is full 718 minus the 617 genesis-valid
  base (small flow measures 609); 644 and 718 differ by 74 and that pair
  is rec2-vs-full, not the +101 item.

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
  | M-T9 | window_flags bit 7 tied to 0 | test_top_skid_overflow_e2e / verilator (flags 0x00) |

- Gate at build: `make lint` clean; `make test-full` 258 passed
  (30:24); `make repro` (SEED=42) 258 passed (30:14); repo guard 5
  passed; `grep MUTANT` clean. M-T9 applied live this commit: killed
  by `test_top_skid_overflow_e2e` / verilator (`0x0` at
  `flags & 0x80 == 0x80`), reverted.
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

## Week 5d3: keyless/sampleless verifier + RTL ledger fixtures (done)

### Verifier contract (ledger/verifier.py)

- Holds ONLY the Ed25519 public key (+ optional anchor). Never sees
  samples, the HMAC key, samples-side stats. Every integrity binding the
  FPGA published is covered transitively by
  `SIG = Ed25519(HMAC || descriptor || window_hash)`: descriptor and hash
  directly, HMAC because it sits inside the signing preimage — the
  verifier cannot recompute it, so the capability matrix states "HMAC
  bound, not HMAC-verifying" (Amendment 2).
- First-failure + all-failures (Amendment 1): per-record checks run in a
  fixed canonical order; the FIRST failing check is the headline verdict
  AND every failing check is collected (`RecordOutcome.first_verdict` +
  `failures`). Canonical order (final, code + docstring agree):
  malformed, bad-device-id, bad-descriptor, bad-overrun,
  bad-overrun-range, **bad-bit7**, **bad-genesis (BEFORE bad-chain — a
  second genesis reads as a reset event)**, bad-chain, bad-replay,
  bad-continuity, bad-gap, bad-invalid-count, bad-neg-clamp, bad-range,
  bad-signature.
- Classification (Q1/Q2/Q3): bit 7 = hard `bad-bit7` reject, never
  excluded (Rule: "not valid, never into baseline/aggregation");
  `classify()` = valid | excluded (bit 5 NEG clamp or all-invalid record)
  | rejected. `classify()` must import from a clean interpreter (no
  numpy). Verifier works pubkey-only (no HMAC/secret attributes).
- Bit-7 records SKIP the field-level checks (bad-invalid-count /
  bad-neg-clamp / bad-range) when bit 7 is set, so an RTL overflow
  fixture row fails ONLY `bad-bit7` (first verdict) and nothing else.
- Gap rule (implemented): `gap = next_wstart − (prev_wstart +
  prev_attested_span)`; bit 6 clear ⇒ gap == 0 required; bit 6 set ⇒
  gap ≥ count × 11 (GAP_MIN_DROP_SPAN) with count ≤ 5. Distinguishes a
  dropped-window jump from a deleted-window (tamper) jump.
- Anchor: `expected_last_counter`/head hash. Above the tail →
  `tail-truncated`; records overshooting it → `anchor-mismatch`; none →
  `unverified` tail (documented miss, Amendment 2 of 5d2).
- Reset events: any counter == 0 / zero-prev genesis NOT at the head, OR
  a genesis sync, marks the ledger NOT acceptable for issuance while
  per-record classification continues (Amendment 3: counter halts and
  never reverts, so a genesis mid-ledger = physical reset).
- Writer seam (`ledger/writer.py`): `read_ledger`/`write_ledger`, writer
  REFUSES a descriptor round-trip mismatch and a chain break (WriterError)
  and refuses a bad HMAC at attestation (SEError) — the writer is
  trusted-for-export glue, the VERIFIER is the untrusted-side gate.

### RTL fixtures: oracle semantics, not samples

- Two generator nodes (`tb/w5d_ledger_cocotb.py`), committed fixtures in
  `tests/fixtures/` are the SOURCE OF TRUTH: regeneration writes to
  `results/gen_*.jsonl` and MUST byte-equal the committed file (sim +
  writer are deterministic at fixed seed); absent fixture → BOOTSTRAP
  write (fresh clone), then every later run is a byte-compare. Pins in
  `tests/test_week5d3_fixtures.py` (SHA-256 constants; any re-rotation of
  the fixture bumps BOTH the file and the constant, never silently).
- `ledger_rtl_clean.jsonl`: 4 records, M ≈ 2000 in range (M_MIN/M_MAX
  gold rule ON, `check_zc_range=True` — the RTL-world proof of the range
  rule), flags 0x1f, cumulative energy monotone, gaps 0.
- `ledger_rtl_overflow.jsonl`: 2 records at SKID_DEPTH 4, flags 0x80,
  zc intact, energy 0 then unchanged, p_avg 0 — from the 5d2c overflow
  proof geometry; rejected as `bad-bit7` and nothing else.

### Tamper / uniqueness coverage

- Tamper suite (table-driven on the RTL fixture): deleted, reordered,
  head-truncated, duplicated middle, duplicated GENESIS row, replayed
  old (chain-patched), second genesis, edited value, swapped HMAC,
  swapped signatures, counter re-mint, window_start re-mint, bit5 +
  nonzero energy, bit6-no-count, overrun 6, bit7 flag, wrong pubkey.
  Documented behaviors: a duplicated genesis row reads as a RESET event
  and `bad-genesis` fires first (before bad-chain — collision decision
  recorded); `test_tail_truncation_needs_anchor_documented_miss` pins the
  no-anchor miss so future anchor work cannot claim it silently.
- Re-minted rows (`re_mint` in `tests/week5d3_support.py`, white-box
  re-sign) are UNIT-level only — the RTL cannot produce them (Amendment 3).
- Uniqueness run (10,000 records = 50,000 windows ≈ 2.8 h at 1 Hz,
  SEED=42): counter 0..9999 exactly once, chain hash links every
  segment, all 10,000 sigs/hashes/HMACs pairwise-distinct, ~1/17 records
  carry one invalid sub-window, one all-invalid record, 1 % real
  in-range windows. Range check OFF via `check_zc_range=False`
  (Amendment 5 divergence): the RTL clean fixture + a dedicated small
  chain re-prove the range rule with it ON. DIVERGENCE (documented, not
  hidden): the golden declares chronology following ATTESTED span only —
  invalid sub-windows advance no declared timeline, so bit-6-clear gaps
  are exactly 0; the strict rule would over-reject RTL natural-invalid
  windows (a physically-dropped-window gap looks valid when the drop is
  natural, and that ambiguity is inherent — the bit-6+count attribution
  is the only distinguisher).

### Mutation suite (M-5d3-1..7, all caught, none survive)

- Each mutant is a ONE-LINE edit to a COPY of `ledger/verifier.py`
  outside the repo tree (`results/mutation/`), imported under a unique
  module name; the repo file is never touched (`grep MUTANT` clean, and
  the file-loading path is itself asserted). Catchers are named real test
  nodes (provenance test).
  | # | mutant | first catcher |
  |---|---|---|
  | M-5d3-1 | signature check removed | `test_wrong_pubkey_detects_every_signature` |
  | M-5d3-2 | gap `>=` → `>` | `test_gap_bit6_drop_below_min_span_fails_but_boundary_passes` |
  | M-5d3-3 | bit6-iff-count check removed | `test_tamper_verdict[bit6 set, count 0]` |
  | M-5d3-4 | bit-7 hard-reject removed | `test_overflow_fixture_rows_reject_as_bit7` |
  | M-5d3-5 | anchor ignored | `test_tail_truncation_caught_with_anchor` |
  | M-5d3-6 | first-record genesis check removed | `test_tamper_verdict[head truncation]` |
  | M-5d3-7 | classify() treats bit-7 as valid | `test_classify_classes` |
- Bring-up learning (kept honest): the first M-5d3-1 ("drop HMAC from the
  sig preimage") was a NEUTRAL mutant — a swapped-hmac-tag catcher can
  never fire on it because the exchanged hmac is already unauthenticated
  (capability matrix) and the sig still covers desc+hash, so the row
  still fails on the true preimage; replaced by the removable-check
  mutant. First-failure assertion for M-5d3-4 must read the per-record
  `first_verdict`, NOT `counts["rejected"]` — counts bucket through
  `classify()` (bit-7-aware), which still rejects the overflow rows even
  with the in-loop rule dead.

## Week 6D: provisional torque criterion and capability representation (approved gate)

- Provisional simulation acceptance criterion (unchanged): instantaneous
  electromagnetic torque `T_e(t) >= 0.9 * T_load + TORQUE_SAFETY_OFFSET`
  per sample. Rationale: preserves the ripple-aware safety bar; the 1 s
  average is reported but never accepted as proof of instantaneous safety.
- Scope limits of that criterion: `T_e` is air-gap torque from the
  equivalent circuit, NOT independently validated shaft torque (the rigid
  single-inertia identity cannot validate a physical shaft); delivered
  torque is UNAVAILABLE as an independent quantity. Speed/stall thresholds
  are NOT established (need product/safety requirements + load data).
- MPC/failsafe torque constraint (Week 6D): derated peak-torque capability
  `L(u) * 0.7 >= 0.9 * load + 0.7` with `L(u) = -12.571547 + 38.428309 * u`
  [N*m], least-squares fit of circuit peak torque over u in [0.4, 1.0],
  worst measured derated overestimation 0.6519 covered by offset 0.7.
  Rationale: capability (achievable envelope, stall headroom) is what a
  throttle can guarantee; produced-torque compliance is measured
  separately. Affine in u, so the QP stays convex (OSQP behavior
  unchanged). Validity domain: u in [0.4, 1.0], r_scale in [1.0, 1.01];
  outside it, classify infeasible. The pre-Stage-A operating-point
  surrogate is superseded (its calibration basis no longer exists).
- Infeasibility policy: empty feasible set is reported with
  `solver_infeasible` + `safety_ok=false` + fallback u=1.0; never relaxed,
  never reclassified as success.
- Savings estimation: <3% relative target retained; baseline trains on
  legacy u=1.0 data only (throttle coefficient provably unidentifiable:
  fits to exactly 0.0); undefined relative error is null with reason,
  never passing; no absolute-error threshold approved.

## Week 6 interim status (engineering gate, not product safety approval)

- The provisional instantaneous-torque criterion above is an INTERIM
  engineering criterion. The physical quantity constrained by the eventual
  product requirement remains subject to confirmation by the product/safety
  owner; product safety approval has NOT been obtained.
- Speed trajectories, speed drops, slip-limit events, and infeasibility
  diagnostics continue to be collected and reported. They do not
  constitute an approved safety case; numerical speed/stall limits require
  motor/load data plus explicit product/safety approval (none invented).
- Zero or negative ground-truth savings are legitimate diagnostic
  outcomes; they do not satisfy Week 6's energy-saving objective. The
  4-8% figure remains superseded unless a valid reproducible experiment
  independently establishes otherwise.
- Disposition: Week 6 remains NOT ACCEPTED against its original acceptance
  criteria (positive savings, instantaneous torque, <3% estimation).
  MPC constraint, plant, baseline, sensors, RTL, and verifier are frozen
  under this gate; Stage B and Week 7 are not authorized.

## Week 6E requirements finding: torque-criterion arithmetic (needs product/safety decision)

- At torque-balance steady state the coupled plant satisfies
  `T_avg == load + friction` exactly (measured residual <1e-6 N*m at every
  declared load, nominal synthetic scenario J_motor=0.0064/J_load=0.01/
  B=0.0008/Tf=0, r_scale=1.0). Hence the provisional minimum
  `T_min = 0.7 * (L + f)` against required `0.9 * L + 0.2`:

  | load [N*m] | T_avg | friction | T_min = 0.7T | required 0.9L+0.2 | attainable |
  |---|---|---|---|---|---|
  | 6.0 | 6.246 | 0.246 | 4.372 | 5.600 | no |
  | 10.0 | 10.243 | 0.243 | 7.170 | 9.200 | no |
  | 14.0 | 14.239 | 0.239 | 9.967 | 12.800 | no |
  | 17.0 | 17.236 | 0.236 | 12.065 | 15.500 | no |

  Attainability would require `0.7*f >= 0.2*L + 0.2`, false for all
  declared loads at physical friction levels. The criterion is therefore
  structurally unsatisfiable by throttle control at steady state (throttle
  moves speed/slip, not produced torque, which tracks demand by
  equilibrium). No replacement criterion is proposed or adopted here; the
  provisional criterion stands unchanged pending the product/safety owner.
- Test-design finding (same audit): `test_mpc_solve_passes_step_and_time_
  on_timeout` triggers failsafe via a 1 ms wall-clock OSQP timeout, whose
  terminal status (`infeasible` vs time-limited `optimal_inaccurate` /
  `user_limit`) is machine-timing sensitive: 50/50 deterministic
  `infeasible` in isolation and under load, but one observed full-suite
  failure. Production/experiment paths use a 100 ms limit that is inert in
  practice (zero timeouts across all 180 experiment solves; disabling it
  yields bit-identical schedules). A deterministic-test fix without
  `control/mpc.py` behavior change is authorized; the `optimal_inaccurate`
  acceptance policy itself is NOT changed here (options pending owner
  choice).

## Week 6F: optimal_inaccurate policy + iteration-only production (approved)

- `optimal_inaccurate` is ACCEPTED only after re-validation against the
  full failsafe safety check; otherwise the documented fallback runs with
  reason `solver_optimal_inaccurate_revalidation_failed`. Acceptance logs
  `solver_optimal_inaccurate` with a saturating 16-bit counter; `user_limit`
  maps to named `solver_timeout` (never "unknown"). Mutant M-6F-1
  (re-validation skipped) caught by
  `test_revalidation_fail_falls_back_with_distinct_reason`; `grep MUTANT`
  clean.
- Production uses the iteration limit only (`max_iter=4000`,
  `solver_timeout_ms=0`); wall-clock limits are test-only overrides.
  Bit-identical proof (SEED=42, all 3 profiles, 60 s): u schedules, GT
  energies, and failsafe counts identical with limit 100 vs 0.
- SCOPE NOTE on that inertness result: it holds ONLY for 10-step QPs of
  the current formulation on this machine (Apple Silicon, idle-to-moderate
  load). Longer horizons, tighter tolerances, slower hardware, or loaded
  machines may bind a wall-clock limit; any such change re-opens the
  timing-sensitivity question and must re-run the bit-identical proof.

### Week 6I model-validity gap (documentation only; model not modified)

- Core loss in `plant/motor.py` is a fixed constant: `p_core = p.v_rms_nom_V**2 / p.rc_Ohm`
  (file/line `plant/motor.py:224`), inside the input-power split
  `t_avg = (p_in - p_cu - p_core) / omega_sync` (line 225). Real core loss varies with flux
  and frequency (approximately V^2, and proportionally with flux density), so the Week 6
  potential-savings figures may be understated at light load because core loss does not fall with
  reduced slip/current in the way a real machine would. This is a model-validity gap: it requires
  motor datasheet data and an authorized decision before any model change.
- Rotor copper: it is already inside the equivalent-circuit input power because the rotor branch
  `r2 / slip + j*x2` participates in total impedance (plant/motor.py lines 146-172), but it is
  never subtracted as a separate loss in the `p_in - p_cu - p_core` split (lines 222-225); the
  air-gap torque therefore retains rotor copper within `t_avg`.
- Stray loss and windage loss are absent: no terms, coefficients or parameters for either appear
  in `plant/motor.py` or `plant/mechanics.py`.

## Week 6J: requirements and physics plan (owner decisions, 2026-10-13)

Status: PLAN ONLY. No code under `plant/`, `control/`, `baseline/` or `rtl/`
is written in this phase. Stage B and Week 7 are NOT started. The plan is
presented for approval and execution stops here.

The five owner decisions below are recorded VERBATIM (quoted text is the
owner's wording). Items explicitly marked PROPOSED need a separate approval
before any run (the plan proposes them, it does not apply them).

Owner decision 1 — protected quantity (verbatim): "Protected quantity:
instantaneous electromagnetic torque, criterion 0.9*load + 0.2 N·m kept as
written and still marked provisional. Resolution of the structural
infeasibility: option C, torque ripple must be DERIVED from a transient
electromagnetic model, not imposed."

Consequence recorded: the provisional criterion text is UNCHANGED (still
`T_e(t) >= 0.9*T_load + 0.2 N·m` per sample, still provisional). What
changes is the source of `T_e`: it is no longer the quasi-steady circuit
mean times an imposed `0.3*cos(2*omega*t)` ripple; it is the cross product
of the transient dq flux/current states. No tuning of the criterion,
threshold or ripple amplitude is authorized.

Owner decision 2 — motor / measurement (verbatim): "Motor: three-phase
induction motor, dq-frame transient model, synthetic, every claim labeled
'simulation only'. The measurement chain stays single-phase, measuring
phase A under a stated balanced-load assumption, with an optional
supply-unbalance parameter to test that assumption."

Consequence recorded: AGENTS.md "finish single-phase end to end before
touching three-phase" is satisfied — the single-phase measurement chain is
frozen (Weeks 2-5, unchanged). This phase extends the PLANT to three-phase
while the measurement path remains single-phase phase A. Under the
balanced-load assumption the reported power is `3 * mean(v_A * i_A)`; the
optional supply-unbalance parameter exists to test (and quantify the error
in) that assumption, never to hide it.

Owner decision 3 — test cases (verbatim): "Test cases: the existing
constant-torque case, plus a variable-torque (pump/fan) load with speed
control (V/f drive)."

Consequence recorded: the committed constant-torque profiles
(steady/cyclic/bursty) remain as a case; a pump/fan (quadratic) load under
V/f speed control is added. Both are exercised by the torque-criterion
scenarios (startup, load step, speed ramp).

Owner decision 4 — savings-estimate acceptance (verbatim): "Savings-estimate
acceptance: the true savings fall inside the reported confidence interval,
measured as a coverage rate over N seeds. Propose N and the interval level
for approval. The interval must include model-form error."

Consequence recorded: the old "<3% relative error" target is REPLACED as
the acceptance test by interval coverage. N and the interval level are
PROPOSED in the 6J plan (N = 100 seeds, nominal 95% central interval,
empirical coverage gate >= 90/100) and require approval before the 6M run.
The interval must add a model-form-error term derived from physics-test
residuals and stated omitted-effect bounds, never from the savings outcome.

Owner decision 5 — physics standard (verbatim): "Physics standard:
'physically consistent within stated approximations', enforced by tests."

Consequence recorded: every model carries an explicit, test-pinned
approximation list (linear magnetics / no saturation, no skin or deep-bar
effect, ideal sinusoidal supply / no inverter switching harmonics, lumped
thermal without per-sample electro-thermal feedback, fixed core loss until
the proposed flux-dependent term is approved). The tests assert these
approximations are DECLARED, not that they are absent.

Consequences of the owner decisions (verbatim): "all committed Week 6
results (week6c_stageA, week6d, DECISION_MEMO figures) are superseded once
the plant changes. Keep them in git history, label them superseded, do not
delete them. The Week 2 measurement-error metrics must be rerun on the new
waveforms. The RTL and attestation path are unchanged."

Recorded consequences (execution obligations, not yet performed):
- `results/metrics.json` `week6c_stageA`, `week6`, and the
  `docs/DECISION_MEMO.md` figures stay in place and in git history; once
  6M lands they are labelled superseded (new `run_labels` entries and/or a
  superseded marker), never deleted.
- The Week 2 measurement-error metrics (`results/metrics.json` `week`
  section: per-profile `p_mean_pct`/`p_max_pct`, the 1.506 % acceptance
  bound) are re-executed on the new dq-model waveforms (single-phase chain
  unchanged) and the old numbers are labelled superseded.
- `rtl/`, `edge/`, `ledger/` and the attestation byte layouts are
  untouched: the dq plant feeds the same single-phase AFE -> ADC -> framing
  -> signing path.

Plan-level guardrails recorded with this decision set (bind every later
phase): tolerances are fixed and written down before the acceptance run and
before any result is read; the integration step is chosen from the measured
fastest electrical eigenvalue, never from whether a criterion passes; model
parameters are reused from the committed set, never re-fit to make the
criterion pass; every non-measured number carried into a plan or doc is
labelled derived/analytic; scenarios and seeds that fail are reported.


## Week 6J2: plan addendum (owner decisions, 2026-10-13)

Owner decision A — meter placement (verbatim, recommendation): "Meter the
drive's grid-side input at 50 Hz for BOTH arms, so the attestation range
(45-55 Hz, M 1810-2230, M 12-bit) is unchanged and RTL is unchanged. Add a
declared drive-loss model (synthetic, labeled). Document why motor-terminal
metering was rejected (windows invalid below 45 Hz, field overflow below
~24 Hz)."

Consequence recorded: both the legacy (throttle) arm and the retrofit
(VSD) arm are metered on the grid side of the drive, where the supply is a
stiff 50 Hz source, so the zero-crossing window logic and the 45-55 Hz
attestation range and the existing 12-bit sample format are untouched.
Motor-terminal metering is REJECTED on two recorded grounds: (i) the
whole-cycle zero-crossing windows assume 45-55 Hz and are invalid below
45 Hz; (ii) the voltage/current field overflows the 12-bit range below a
derived ~24 Hz minimum. A declared drive-loss model (synthetic, labelled)
relates grid-side input power to motor-terminal power; it is a modelling
term, never a measured claim. The RTL, framing and attestation path are
unchanged.

Owner decision B — torque acceptance (verbatim, recommendation, option A):
"Steady-state samples gate acceptance with zero violations after a declared
settling window; transient violations are reported and localized but do not
gate. Record that startup, load step-up and deceleration violate the
criterion by physics."

Consequence recorded: the torque-criterion acceptance gate is evaluated on
steady-state samples only, after a declared settling window (per scenario);
the gate is zero violations. Transient samples (startup, load step-up,
deceleration) are reported and time-localized but do not gate, and it is
recorded that they violate the criterion by physics (T_e lags the transient
demand). The provisional criterion text `T_e(t) >= 0.9*load + 0.2 N·m` is
unchanged. Only the SAMPLE SET that gates changes; no threshold or ripple
amplitude is tuned.

Owner decision C — legacy vs retrofit (verbatim, for approval): "Legacy
arm: constant-speed motor with throttle valve; retrofit: variable-speed
drive meeting the same demand. Propose the demand profile Q(t), pump and
system curves with a static-head share, the valve model, and the
equal-service rule (equal flow or hydraulic work), for my approval."

Consequence recorded: the savings comparison is between a constant-speed
pump with a throttle valve (legacy) and a VSD meeting the same demand
(retrofit). The demand profile Q(t), the pump curve, the system curve (with
a declared static-head share), the valve model, and the equal-service rule
are PROPOSED below and require approval before 6L. No savings number may be
produced before this is approved.

Plan amendments recorded with this decision set (bind 6K/6L/6M; all numbers
below are labelled measured or derived/analytic):

(1) Complex eigenvalues of the electrical dq state matrix
(A = d(lambda)/dt states [lambda_ds, lambda_qs, lambda_dr, lambda_qr],
omega_e = 2*pi*50 rad/s, committed MotorParams) were recomputed as COMPLEX
values over the slip range. Derived from the committed parameter set
L_s = L_r = 147.0592 mH, L_m = 143.2394 mH, L_ls = L_lr = 3.8197 mH,
sigma = 0.0512734, R1 = 0.4, R2 = 0.48 ohm:

  slip   eigenvalues (complex, rad/s)                  |lambda|max
  0.001  -52.6665 +-303.6096j ; -64.0409 +-10.8638j    308.144
  0.010  -52.6587 +-303.5070j ; -64.0487 +-13.7939j    308.041
  0.040  -52.6310 +-303.1495j ; -64.0764 +-23.5761j    307.684
  0.100  -52.5655 +-302.3540j ; -64.1419 +-43.2212j    306.889
  0.150  -52.4977 +-301.5931j ; -64.2097 +-59.6901j    306.128
  1.000  -1.5030 +-314.1593j ; -115.2044 +-314.1593j   334.616 (locked rotor)

  The worst case is LOCKED ROTOR: |lambda|max = 334.616 rad/s (the running
  eigenvalues are ~308 rad/s). The mechanical pole is -B/J = -0.125 rad/s.

(2) Step gate updated from the complex eigenvalues: require
|lambda|max*dt_int <= 5e-3 with |lambda|max = 334.616 rad/s, so
dt_int <= 1.494e-5 s and f_int >= 66.9 kHz. At the 10 kHz output grid the
minimum sub-stepping is N_sub >= 6.69, i.e. N_sub = 7 (70 kHz,
|lambda|max*dt = 4.78e-3); the default is N_sub = 8 (80 kHz,
|lambda|max*dt = 4.18e-3) for power-of-two headroom. N_sub = 6 (60 kHz,
5.58e-3) FAILS the gate. The earlier N_sub = 4 proposal (based on the real
parts only) is CORRECTED and superseded. The convergence harness compares
N_sub in {4, 8, 16}. Independent of the eigenvalue gate, the model must
also resolve the 2*f_s = 100 Hz unbalance forcing: >= 100 samples per 100 Hz
cycle (>= 10 kHz), satisfied by N_sub >= 1.

(3) Locked-rotor step-response test added: with omega_r = 0 the electrical
subsystem is LTI, so the numerical step response is compared against the
exact matrix exponential solution x(t) = A^-1 (e^{A t} - I) B v. Tolerance
1e-6 relative on all four flux states, derived from RK4 truncation
(~7.5e-12 over the locked-rotor transient, tau = 1/115.2 = 8.68 ms at
80 kHz); a first- or second-order implementation error would show ~1e-3.

(4) Integration scheme pinned: RK4 for the electrical + mechanical states at
dt_int (not semi-implicit Euler as in the committed Stage A). All physics
tests 1-3 are labelled IMPLEMENTATION CHECKS against the SAME circuit
theory, not validation against a real machine.

(5) Per-test numeric tolerances, each with its derivation, fixed at this
decision BEFORE any 6K code (no tolerance may be chosen after seeing a run):

  #  test                                          tol (relative)  derivation
  1  steady-state T_avg vs 3x committed circuit     2e-2    core-loss convention: committed ref subtracts fixed V^2/Rc (1.443 N*m 3-ph = 1.68% of T_max), dq uses a magnetizing-voltage shunt; MEASURED worst discrepancy 8.28e-3 (pull-out) -> 2.4x margin
  1b steady-state T_avg vs analytic shunt Thevenin  1e-3    algebraic model identity; residual = max-search + settling
  2  pull-out T_max vs Thevenin analytic            2e-2    same core-loss convention; MEASURED 8.28e-3 (analytic 85.902 vs circuit 85.196 N*m, s_po = 0.19997)
  3  starting torque (s=1) vs analytic              2e-2    same core-loss convention
  4  electromagnetic power balance                  1e-6    RK4 truncation ~7.5e-12, margin ~1e5
  5  mechanical momentum balance J*dw = int(T_e-load-fric)dt  1e-6  RK4 truncation
  6  no-ripple balanced (p-p over one cycle)        peak-to-peak <= 1e-3*T_avg   RK4 residual ~(|lambda|dt)^2 = 1.75e-5, margin ~57x; PINS that ripple is derived, not imposed
  7  unbalance |T_2we| vs sequence-network formula  1e-2    mechanical back-reaction dw_r/w_e = 2.9e-3 (from dT/(J*2*w_e)/w_e), margin 3.4x
  8  balanced supply -> zero negative sequence      1e-12   exact algebraic property
  9  step convergence N_sub {8 vs 16}              1e-4    RK4 truncation
  10 locked-rotor step vs expm                      1e-6    RK4 truncation ~7.5e-12
  11 |lambda|max*dt_int <= 5e-3                     inequality gate (no tol)
  12 determinism (same seed/inputs)                 exact equality
  13 approximation list declared                    presence check (exact)

(6) Unbalance 2*omega_e torque prediction MUST come from the sequence-network
formula, not the simulation. Recorded formula (small-signal, exact for the
linear electrical system at fixed omega_r; identical to the sequence cross
term (3/2) p L_m | I_p^s conj(I_n^r) + I_n^s conj(I_p^r) |):
T_2we = | C (j*2*omega_e I - A)^-1 B U_n |, where A, B are the dq matrices
built from the committed equivalent-circuit parameters, U_n is the
negative-sequence dq phasor (magnitude V_n = a_u*V_pk), and
C = dT/dx at the operating point. DERIVED values (from the formula, not a
run): at s = 0.04, a_u = 0.02, |T_2we| = 3.6440 N*m (a_u = 0.01 -> 1.8220,
a_u = 0.05 -> 9.1100). A separate nonlinear integration cross-check
(measured, reported only as a check) reproduced these to 5 significant
figures, consistent with the electrical system being linear at fixed speed.

(7) Core loss enters the energy balance as a shunt resistance Rc in the
magnetizing branch of BOTH the d and q axes (parallel with L_m); the sink is
P_core = (3/2)(v_cd i_cd + v_cq i_cq) with i_c = v_c/Rc across the shunt,
NOT the committed fixed P_core = V_rms^2/Rc (terminal), which is retained
only inside the committed reference and is the source of the test-1/2/3
tolerance above. The energy-balance test asserts
P_in = P_cu,s + P_cu,r + P_core + P_mech + d/dt(1/2 L i^2). It PROVES the
integrator conserves energy to the truncation tolerance and that the
declared loss terms are the only accounted sinks. It does NOT prove Rc is
correct, that the hysteresis/eddy split is right, that no losses are omitted
(windage, stray-load), or that the model matches a real machine.

(8) Compute budget (measured where stated). The committed Stage-A
integrator measured at 176,222 samples/s single-threaded (10 kHz) =>
0.0567 s per 1 s arm-run and ~3.40 s per 60 s arm-run; the committed
`week6c_stageA` end-to-end run reproduces in ~25.8 s (from the Week 6H
entry). A cross-seed batched dq core (100 seeds, 80 kHz, electric states
only) measured at 88,890 steps/s => ~54 s for a 60 s batch, vs ~5400 s if
looped one seed at a time. Plan: cross-seed vectorization (batch the
(N_seeds, n_states) arrays; omega_r varies per seed so apply the batched 4x4
via einsum). Estimated 100-seed coverage artifact (both arms, all profiles,
mechanical + control coupling) ~30-90 min: mark it `@pytest.mark.slow`
writing a dedicated coverage artifact; `make repro` runs a 5-seed
verification subset that must reproduce the artifact's first 5 seeds
exactly.

(9) V/f low-frequency boost DERIVED from R1 and rated current, not the
earlier arbitrary 20 V. At rated (s = 0.04, V = 230 V, committed params)
I_rms = 19.4019 A, so the per-phase resistive boost is
V_boost = I_rated*R1 = 19.4019 * 0.4 = 7.7607 V rms (10.975 V peak). The
leakage drop I*X1 = 23.28 V rms scales with f and is negligible at low f.
The drive model uses this derived boost; 20 V is NOT used.

(10) Core loss, PROPOSED for application in 6L, NOT applied in 6J2. Form
(Steinmetz + eddy): P_core = k_h f B^alpha + k_e f^2 B^2, B proportional to
lambda, i.e. P_core = P_core_nom (lambda/lambda_nom)^2 * [(1-c) (f/f_nom)
+ c (f/f_nom)^2] for a hysteresis fraction (1-c) and eddy fraction c.
Synthetic, uncalibrated parameters PROPOSED: P_core_nom = 151.143 W
(derived from the committed Rc = 350 ohm at 230 V), alpha = 2, eddy
fraction c = 0.5. A +/-50% sensitivity run on (k_h, k_e) is required in 6L
and must report the effect on torque, energy and coverage. Until approved,
approximation #5 (fixed core loss) stands.

(11) Half-width cap (anti-gaming), numeric formula derived from the sensor
spec bound and the baseline error, as a fraction of legacy energy E_leg:
  cap = sqrt(sensor_spec^2 + cv_rmse^2) * E_leg
      = sqrt(0.01506^2 + 0.07646^2) * E_leg = 0.07793 * E_leg  (7.79%, RSS)
  conservative additive alternative = (0.01506 + 0.07646) = 0.09152 * E_leg
(9.15%). sensor_spec = 1.506% is the committed acceptance bound
(results/metrics.json acceptance_bound_p_mean_pct); cv_rmse = 7.646% is the
committed baseline CV(RMSE) (DECISION_MEMO section 6). Basis (per-window
energy vs total-energy estimator error) is PROPOSED; if the CI exceeds the
cap the run is not accepted regardless of coverage. Reported honestly: the
cap is ~100x the committed 0.02-0.09% savings scale, so it is a sanity
bound, not the load-bearing gate.

(12) Coverage reporting: report coverage WITH and WITHOUT the model-form
term, and the fraction of seeds whose CI EXCLUDES zero. The model-form term
is added to the CI before the run, from physics-test residuals plus stated
omitted-effect bounds, never from the savings outcome.

(13) Unchanged scope guardrails: no code under plant/, control/, baseline/,
rtl/, sim/ or tests/ in 6J2; 6K code, Stage B and Week 7 are NOT started;
tolerances fixed before any run; no tuning against ground truth; every
number labelled derived or measured.

Fold-in, round 2 (items a-h), recorded before the week6j2 commit:

(a) Scratch dq prototype: it lived ONLY as an ephemeral in-session Python
script executed in the planning shell (`uv run python - <<'PY'`) — NOT
committed, NOT under plant/ control/ baseline/ rtl/ sim/ tests/. It measured
the 4x4 electrical eigenvalues, the unbalance 2*omega_e torque transfer, the
committed-integrator throughput and the batched-dq benchmark. Those are RUN
OUTPUTS, so they are reportable as measurements but MUST NOT set any
tolerance. Tolerance 1 is set from the ANALYTIC convention gap ALONE: the
committed reference subtracts a fixed per-phase core loss V^2/Rc =
151.1429 W, a 3-phase torque offset 3*V^2/Rc/omega_e = 1.44331 N*m = 1.68%
of T_max = 85.902 N*m (all derived from V, Rc, omega_e, T_max — no run).
tol_1 = 1.7e-2 (the 1.68% gap rounded up). Rule recorded: tolerance values
must not come from run outputs.

(b) Tests 2 and 3 compare against the ANALYTIC Thevenin torque with the SAME
shunt-Rc convention, tolerance 1e-3 (algebraic model identity; residual =
max-search + settling). The comparison against the COMMITTED circuit (fixed
V^2/Rc convention) is retained as INFORMATIONAL with bound 2e-2 and does not
gate.

(c) Exact dq equations WITH the shunt Rc branch. Seven states:
[lambda_ds, lambda_qs, lambda_dr, lambda_qr, lambda_md, lambda_mq, omega_m]
(6 electrical + 1 mechanical). Leakage/magnetizing split (SI, p = 1):
  lambda_ds = L_ls i_ds + lambda_md ;  lambda_qs = L_ls i_qs + lambda_mq
  lambda_dr = L_lr i_dr + lambda_md ;  lambda_qr = L_lr i_qr + lambda_mq
  i_ds = (lambda_ds - lambda_md)/L_ls ;  i_qs = (lambda_qs - lambda_mq)/L_ls
  i_dr = (lambda_dr - lambda_md)/L_lr ;  i_qr = (lambda_qr - lambda_mq)/L_lr
Stator/rotor flux (synchronous frame, omega_e = 2*pi*f_s, omega_r = p*omega_m):
  dlambda_ds/dt = v_ds - R_s i_ds + omega_e lambda_qs
  dlambda_qs/dt = v_qs - R_s i_qs - omega_e lambda_ds
  dlambda_dr/dt = -R_r i_dr + (omega_e - omega_r) lambda_qr
  dlambda_qr/dt = -R_r i_qr - (omega_e - omega_r) lambda_dr
Magnetizing-node KCL (stator current = magnetizing + core-loss + rotor):
  i_ds = i_md + i_cd + i_dr ;  i_qs = i_mq + i_cq + i_qr
  i_md = lambda_md/L_m ;  i_mq = lambda_mq/L_m
  core-loss current driven by the magnetizing branch rotating-frame voltage
  v_m = dlambda_m/dt + j omega_e lambda_m:
  i_cd = (dlambda_md/dt - omega_e lambda_mq)/R_c
  i_cq = (dlambda_mq/dt + omega_e lambda_md)/R_c
  solved EXPLICITLY for the derivative:
  dlambda_md/dt = R_c (i_ds - i_dr - lambda_md/L_m) + omega_e lambda_mq
  dlambda_mq/dt = R_c (i_qs - i_qr - lambda_mq/L_m) - omega_e lambda_md
Derived torque: T_e = (3/2) p (lambda_md i_qs - lambda_mq i_ds) [N*m].
Mechanical: J domega_m/dt = T_e - T_load - B omega_m - Tf sign(omega_m).
How RK4 handles the "implicit" term: writing i_c = v_m/R_c with
v_m = dlambda_m/dt naively puts the derivative on both sides. Carrying
lambda_md, lambda_mq as STATES and using the node KCL to solve for
dlambda_m/dt in closed form (above) makes the system an EXPLICIT ODE, so
RK4 needs no implicit solve; the only coupling is the linear algebraic
leakage/magnetizing relation, eliminated in closed form. The steady-state
consequence is correct: on the d axis i_cd = 0 and on the q axis i_cq =
omega_e lambda_md/R_c, giving a non-zero core loss under balanced supply.

(d) Tolerances for tests 4, 5, 9, 10 and completion of test 6:
 - tests 4 (power balance) and 5 (momentum balance): tol 1e-6 relative over
   a 1 s window. Derived: RK4 local truncation O((|lambda|dt)^5) per step,
   accumulated over N = T_w/dt steps = T_w |lambda|^5 dt^4 =
   1 * 334.616^5 * (1.25e-5)^4 = 1.0e-7, so 1e-6 has ~10x margin. That it
   catches a 1e-6 error is SHOWN BY MUTATION: a 1e-6 relative perturbation
   of a loss term adds ~1e-6 to the residual, which on the ~1e-7 base
   exceeds the 1e-6 tolerance and trips the test.
 - test 9 (step convergence N_sub 8 vs 16): TIGHTENED from 1e-4 to 1e-6
   relative. Derived: the difference between the two solutions is
   O((|lambda|dt)^4) = (4.18e-3)^4 = 3.1e-10, so 1e-6 has ~3000x margin and
   catches a 1e-6 error.
 - test 10 (locked-rotor vs expm): tol 1e-6 relative over t in [0, 0.05 s]
   (~5.8 tau_fast). Derived: RK4 global ~ N (|lambda|dt)^5/120 =
   4000*(4.18e-3)^5/120 = 4.3e-8, so 1e-6 has ~23x margin; a 1e-6
   implementation error dominates the 4.3e-8 base and is caught (SHOWN BY
   MUTATION).
 - test 6 (no-ripple balanced) derivation COMPLETED: a balanced sinusoidal
   supply gives v_ds, v_qs CONSTANT in the synchronous frame, so the steady
   state is an exact DC fixed point of the 6-state linear electrical system
   and T_e is exactly constant; the only ripple sources are a settling
   transient from a non-equilibrium initial condition and roundoff. The
   test starts at the analytic equilibrium (solve A x0 = -B v) and checks
   peak-to-peak T_e over one fundamental cycle: tol peak-to-peak <=
   1e-6 * T_avg. Derived residual = roundoff (~1e-12) + solve roundoff
   (~1e-14); 1e-6 has >1e5 margin. It PINS that no 0.3 ripple is imposed
   (a re-imposed ripple is O(0.3), ~3e5x the tolerance).

(e) Settling-window formula. t_settle = k_s * tau_slow, k_s = 5,
   tau_slow = max(1/min|Re lambda|_OP, J/B_eff,OP), evaluated at the FINAL
   operating point; B_eff = B + dT_load/domega - dT_e/domega. Numbers
   (derived): running OP 1/min|Re lambda| = 19.05 ms; dT_e/domega_r =
   -2.4874 N*m*s/rad (from the circuit); J/B_eff = 2.57 ms (const-torque)
   / 2.49 ms (pump) -> tau_slow = 19.05 ms -> t_settle = 95 ms (declare
   0.10 s). VSD ramp: measure t_settle from t_ramp_end (declared ramp rate
   30 rad/s^2). Startup: measure from the first instant
   |omega - omega_final|/omega_final <= 1%. Steady DEFINITION: a sample is
   steady iff (1) t >= t_edge + t_settle, where t_edge is the last applied
   edge (step time / ramp end / startup band entry); (2) the load is
   constant over [t - t_settle, t]; (3) |omega_m - omega_ref|/omega_ref <=
   1%. Only steady samples gate.

(f) Pump additions (decision C):
   - Pump efficiency eta(Q,n): eta = eta_max [1 - kappa (Q/Q_BEP(n) - 1)^2],
     clipped to [0, eta_max]; Q_BEP(n) = Q_BEP,nom (n/n_nom); eta_max =
     0.75, kappa = 0.5 (synthetic). P_hyd = rho g Q H; P_shaft =
     P_hyd/eta(Q,n).
   - Retrofit speed is SOLVED, not assumed Q ~ n: for each demanded Q_d(t),
     solve for n from H_p(Q_d(t); n) = H_sys(Q_d(t)) with the
     affinity-scaled pump curve H_p(Q;n) = H_p,nom(Q n_nom/n)(n/n_nom)^2.
     (Q ~ n is rejected: it is the zero-static-head limit only.)
   - Motor/pump sizing ratio DECLARED: rho g Q_d H_d = 1962 W hydraulic;
     shaft at eta = 0.75 = 2616 W; motor rated = 1.15x = 3008 W ~ 3 kW
     (k_size = 1.15). Synthetic.
   - Grid-side current waveform (drive arm), measured phase A at 50 Hz per
     decision A: i_A(t) = I_1 sin(omega_1 t - phi_1) + I_5 sin(5 omega_1 t
     + theta_5) + I_7 sin(7 omega_1 t + theta_7), I_5/I_1 = 0.30,
     I_7/I_1 = 0.15 (typical 6-pulse), displacement PF cos(phi_1) = 0.95;
     I_1 set so grid-side real power = P_shaft/eta_drive with eta_drive =
     0.95 (declared synthetic). Legacy arm is a plain 50 Hz sinusoid. The
     whole-cycle windows average the harmonics over 1 s.
   - Flow in the baseline (PROPOSE, do NOT choose): options (i) replace
     shaft load by delivered flow Q in the regression; (ii) use hydraulic
     work W = rho g int Q H dt; (iii) keep a load regressor derived from
     (Q, n). Trust assumption each adds: flow is an ADDITIONAL sensor NOT
     covered by the electrical attestation chain, so the baseline would
     trust an untrusted flow signal an attacker could spoof. Recorded as a
     new trust-boundary item for owner selection.
   - Design/curve values unchanged from round 1: Q_design = 0.01 m^3/s,
     H_design = 20 m, s0 = 0.35 -> H_static = 7 m, R_s = 130000, H0 =
     26 m, a = 60000; demand Q_d(t) 0-10 s 0.5 Q_d, 10-40 s Q_d, 40-55 s
     0.75 Q_d, 55-60 s 0.3 Q_d; equal-service rule (primary) equal
     instantaneous flow, secondary equal hydraulic work rho g int Q H dt.

(g) What the 100 seeds randomize: each seed drives (a) the AFE analogue
noise realization, (b) the ADC dither/quantization realization, and (c) the
load-profile RNG where applicable (bursty draws; any declared demand
jitter). The plant and controller are deterministic functions of the
seed-driven inputs; dq integration is fixed-step and deterministic.
Cap text CORRECTED: the committed Week 6 savings are 0.37-1.57% of legacy
energy (the earlier "0.02-0.09%" was wrong). Analytic predicted pump
savings computed BEFORE any run, DERIVED from the affinity/valve model with
constant eta: 18.78% of legacy hydraulic energy (per demand segment: 0.5
Q_d 58.2%, Q_d 0.0%, 0.75 Q_d 36.7%, 0.3 Q_d 67.9%; time-weighted 18.78%).
Cap PROPOSED as a fraction of it: cap = alpha * predicted_savings, alpha =
0.5 -> 9.4% of legacy energy; the sensor/baseline RSS bound (7.79%) is
tighter, so the effective cap = min(7.79%, 9.4%) = 7.79% of legacy energy.
Both the predicted savings and the cap are DERIVED, not measured.

(h) Core-loss unit: P_core_nom = 151.1429 W is PER PHASE (V_phase^2/R_c =
230^2/350); the total 3-phase core loss is 3x = 453.429 W. Every core-loss
figure is labelled per-phase or total explicitly.

STATUS: 6J2 addendum (rounds 1 and 2) ready to commit as `week6j2` (docs
only). Part B (6K code) proceeds after this commit. Items awaiting owner
sign-off: decision C (pump/demand/valve/equal-service), amendment 10
(core-loss application in 6L), the flow-in-baseline baseline option (f), and
the cap basis (g).

## Week 6K: dq model findings (code, 2026-10-13)

Code: `plant/dq_machine.py`, `tests/test_week6k_dq.py`, `sim/run_week6k.py`,
`sim/mutants_week6k.py`; metrics under `results/metrics.json['week6k_dq']`.
Committed as `week6k`. The committed tolerance table (6J2 fold-in, item d) is
used UNCHANGED; nothing here was tuned to a run.

(i) The committed 7-state explicit shunt-R_c model is STIFF. Carrying
lambda_md, lambda_mq as states with i_c = (dlambda_m/dt + j omega_e lambda_m)
/R_c gives a pole ~1.86e5 rad/s (from the 6-state linearisation), so at the
committed N_sub = 8 step (80 kHz) the committed step gate is violated by
~460x. The committed (d) step-gate derivation used |lambda|max = 334.616
rad/s, which is the 4-STATE value; the committed plan is therefore internally
inconsistent. RESOLUTION: integrate a QUASI-STATIC 4-state reduction in which
the core-loss current is driven by the rotational EMF only, i_c =
j omega_e lambda_m / R_c. This reproduces the committed 4-state eigenvalue
table basis and the shunt steady state exactly (1e-15) and keeps
|lambda|max * dt_int = 4.18e-3 <= 5e-3. Declared as an approximation; the
dropped term makes the integral energy residual ~1.6e-6 over a 1 s
full-load step (vs the 1e-6 tolerance) while a small-signal 1 Hz ripple gives
~1e-10. Tests 4/5 use the ripple; the step residual is reported, not hidden.

(ii) The committed derived torque T_e = (3/2) p (lambda_md i_qs -
lambda_mq i_ds) is the NO-core-loss identity; with the shunt it overstates
torque (37.35 vs 36.10 N*m at s = 0.04) and fails the committed test 1
(4.0% > 1.7e-2). CORRECTION to committed item (c): use the rotor-current
(air-gap) form T_e = (3/2) p (lambda_mq i_dr - lambda_md i_qr).

(iii) The committed (1) eigenvalue table is the R_C-REMOVED 4-state matrix
(matches to 4 decimals). The implemented shunt model shifts the running
eigenvalues by ~0.1% (s = 0.04: |lambda|max 307.765 vs 307.684); the step
gate uses the implemented value 334.618 rad/s. Both pass.

(iv) The committed tol_1 = 1.7e-2 came from the fixed-V^2/R_c core-loss
offset (1.68% of T_max). The two models SHARE a core loss, so the actual gap
is ~0.5-0.8% over the operating range and grows as T -> 0: measured 8.7e-2 at
s = 0.001 and 3.1e-2 at s = 1.0 (vs the informational 2e-2 bound, non-gating).
Test 1 therefore gates over the committed operating range s in [0.02, 0.25]
(worst 8.30e-3, reproducing the committed 8.28e-3); the full curve is in the
metrics.

(v) The committed (6) unbalance prediction |T_2we| = 3.6440 N*m at s = 0.04,
a_u = 0.02 is NOT reproducible from this model (this run: 1.292 N*m, a factor
~2.82). Test 7 is therefore implemented as a SELF-CONSISTENCY check (the
linearised C (j2we I - A)^-1 B U prediction vs the nonlinear run, 1e-2). The
committed value is reported and not chased.

(vi) The nonlinear flux RHS is now single-sourced in `_electrical_rhs()`. It
was previously duplicated in `electrical_matrix()`, which hid a sign error
from every steady-state test (found by mutant M-6K-3). Mutants M-6K-1..6 are
all caught; the observed first catchers are recorded in the metrics and
reproduced by `make mutants-week6k`.

(vii) `simulate` writes NaN-free strict JSON; the negative-sequence test uses
a fundamental-cycle DFT (Fortescue), 2.8e-16 balanced.

STATUS: 6K committed as `week6k`.

## Week 6L/6M: owner sign-offs and 6M pre-registration (owner decisions, 2026-10-16)

Sign-offs recorded on 2026-10-16 (each was a PROPOSED item awaiting approval;
the quoted reasoning is this record, the approval is the owner's):

1. Findings (i)/(ii) APPROVED as corrections to committed items (c)/(d). The
   quasi-static 4-state reduction replaces the committed explicit 7-state
   shunt-Rc form, and the rotor-current (air-gap) torque form replaces the
   committed derived torque. Committed items (c)/(d) are labelled SUPERSEDED
   and kept in git history, never deleted.
2. Decision C pump case APPROVED AS PROPOSED: demand Q_d(t), pump/system
   curves with static-head share, valve model, eta(Q,n) model, equal-flow
   primary / equal-hydraulic-work secondary rule, 3 kW declared sizing
   (k_size = 1.15). The 18.78% analytic predicted pump savings stays a
   PRE-REGISTERED REFERENCE (derived, not a target), never a gate.
3. Amendment 10 APPROVED for 6L: flux-dependent core loss
   P_core = P_core_nom (lambda/lambda_nom)^2 [(1-c)(f/f_nom) + c(f/f_nom)^2],
   synthetic P_core_nom = 151.143 W per phase, alpha = 2, eddy fraction
   c = 0.5, plus the required +/-50% sensitivity run on (k_h, k_e) reporting
   effects on torque, energy and coverage. Until this lands, approximation #5
   (fixed core loss) stands; this phase applies the approved form.
4. Flow-in-baseline: ELECTRICAL-ONLY selected. No flow regressor; no new trust
   assumption is added. Flow is used only to state the equal-service demand
   rule; the regression stays on the electrical measurement.
5. Coverage/acceptance setup APPROVED, with the owner's documentation request
   answered below: the >=90/100 gate for a nominal 95% interval is reasonable
   (binomial mean 95, SD = sqrt(100*0.95*0.05) = 2.179, so (95-90)/2.179 = 2.29
   SD below the mean — it rarely fails by chance), and both cap numbers are
   derived in this pre-registration BEFORE the run.
6. Unbalance reference DROPPED: the committed |T_2we| = 3.6440 N*m is labelled
   non-reproduced (this model: 1.292 N*m); no unbalance spec is enforced and
   test 7 stays a SELF-CONSISTENCY check.
7. Test-1 range gate CONFIRMED over s in [0.02, 0.25] (worst 8.30e-3 vs tol
   1.7e-2); the full curve is recorded in the metrics, non-gating.
8. Stage B: SKIPPED. 6M runs next, then the project stops and reports before
   Weeks 7/8.
9. Pacing: 6L and 6M run straight through, then STOP AND REPORT. 6M is a
   one-shot pre-registered run; its result decides the Week 6 disposition and
   what Weeks 7/8 build on.

### 6M pre-registration (fixed before any 6M result is read)

Acceptance is INTERVAL COVERAGE (owner decision 4), not a point target.

- N = 100 seeds.
- Interval: nominal 95% central.
- Model-form term: added to the CI BEFORE the run, from physics-test
  residuals plus stated omitted-effect bounds, never from the savings outcome.
- Coverage gate: empirical coverage >= 90/100.
- CI-excludes-zero fraction: reported with and without the model-form term.
- Cap (HALF-WIDTH cap on the total CI including the model-form term, as a
  fraction of legacy energy E_leg; it limits UNCERTAINTY, not the point
  estimate, and exceeding it fails the run regardless of coverage):
  - sensor/baseline RSS bound: sqrt(1.506%^2 + 7.646%^2)
    = sqrt(0.01506^2 + 0.07646^2) = sqrt(0.0002268 + 0.0058461)
    = sqrt(0.0060729) = 0.07793 = 7.79% of E_leg
    (1.506% = committed acceptance_bound_p_mean_pct;
     7.646% = committed baseline CV(RMSE)).
  - predicted-savings bound: 0.5 * 18.78% = 9.4% of E_leg (uncertainty must be
    smaller than half the predicted effect).
  - effective cap = min(7.79%, 9.4%) = 7.79% of legacy energy.
  - The cap applies to the CI HALF-WIDTH, not the point estimate: an
    ~18.78% savings estimate with a +/-2-3% interval passes; the cap only
    kills runs whose uncertainty is absurdly wide (anti-gaming).
- Seeds: the 100 seeds drive (a) the AFE analogue noise realization, (b) the
  ADC dither/quantization realization, and (c) the load-profile RNG (bursty
  draws, declared demand jitter). Plant and controller are deterministic
  functions of the seed-driven inputs; dq integration is fixed-step and
  deterministic.
- Artifacts: `week6` and `week6c_stageA` stay in place and in git history;
  once 6M lands they are labelled SUPERSEDED (new `run_labels` entries and/or
  a superseded marker), never deleted. The Week 2 measurement-error metrics
  are re-executed on the new dq-model waveforms and the old numbers labelled
  superseded. `rtl/`, `edge/`, `ledger/` and the attestation byte layouts are
  untouched: the dq plant feeds the same single-phase AFE -> ADC -> framing
  -> signing path.
- Compute: the 100-seed coverage artifact is marked `@pytest.mark.slow`;
  `make repro` runs a 5-seed subset that must reproduce the artifact's first
  5 seeds exactly (cross-seed vectorization per 6J2 item (8)).

STATUS: sign-offs and 6M pre-registration committed (docs only). 6L (core
loss) then 6M (one-shot pre-registered run) proceed; Stage B and Weeks 7/8 are
not started.
