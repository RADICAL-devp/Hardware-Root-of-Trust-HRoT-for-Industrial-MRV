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

## Week 5b (forward rules for top.v — decided now, wired later)

- Counter consumption rule: `top.v` latches both counters AND
  `cnt_saturated` into per-window snapshot registers at each attestation
  window boundary and emits the delta (snapshot minus previous snapshot);
  the `frame_rx` counters themselves are never cleared except by reset.
  A window whose latched `cnt_saturated` is set reports its counts as
  LOWER BOUNDS, not exact (events past 0xFFFF are uncounted by
  construction). Rationale: latch-the-delta was chosen over
  read-and-clear because a skipped or repeated window boundary can never
  lose counts that way; the sticky flag makes the bound explicit.
- `framing_error` rule: `uart_rx.framing_error` has NO path into
  `frame_rx` — there is nothing to wire in `top.v`. A bad stop bit
  deletes exactly one byte from the byte stream by construction of
  `uart_rx` (byte dropped, pulse raised for telemetry only); `frame_rx`
  sees the holed stream and resyncs via CRC, proven by the hole test.
  `top.v` MAY count `framing_error` pulses per window as link-health
  telemetry; that choice is left to the `top.v` step. Rationale: the hole
  is a byte-stream property, so byte-level proof is the whole proof.
- Overrun argument, one line: worst-case rescan replay stall is 0 cycles
  against an 80-clock byte period, so overrun is structurally impossible.
