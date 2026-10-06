# PROGRESS

## Week 4b2: review fixes (done, awaiting re-review)

- Done (all 8 review items): (1) divider/sqrt declared combinational in
  `rtl/power_calc.v` + DECISIONS.md Week 4b2 — "3 cycles" is behavioral-sim
  depth only, Week 5 builds divider/sqrt FSMs and re-states the count;
  `results/week4b.json` + the Week 4b note above reworded accordingly.
  (2) Descriptor P_AVG source DECIDED: `p_avg_exact` only (DECISIONS.md,
  comments in `edge/attestation.py` + `tb/golden.py`; both were already
  exact-only — verified zero LUT references in `edge/`); `p_avg_lut`
  behind `LUT_MEAN_ENABLE` (0 ties it to 0), proven by the new
  exact-only-build test both sims. (3) Record-level NEG_ENERGY clamp now in
  RTL (`rtl/record_agg.v`: signed increments over valid slots + prev added
  once + clamp once → 0 + flags bit 5; carries `rec_p_sum`/`rec_m_total`/
  5×M/flags for the descriptor); mixed-sign (+,+,-,-,+) records proven for
  negative AND positive totals plus exact-zero (no flag) vs
  `record_fields`/`build_record` byte-for-byte, back-to-back + reset.
  (4) Clock/baud stated (TB 10 ns, 16× → 6.25 Mbps sim; L0 needs ≥ 1.1 Mbps
  = 11 B × 10 b × 10 kHz, so 115200 baud cannot carry it); baud edge
  MEASURED both sims (pass −3.5 %/+5 %, fail −4 %/+5.5 %, ±2 % pinned with
  ≥1.5 pp margin); `frame_rx.v` added to Week 5 scope (DECISIONS.md).
  (5) 88-bit truncation mutant SURVIVES — honestly reported: no in-contract
  test can observe it (max product 2.22e16 < 2^64; invalid-window garbage
  forced to 0); 88-bit stays as defense-in-depth with the bound in a
  comment. (6) Trailing `win_start_pulse` confirmed as original Week 4a2
  spec ("E owned by the next window") in DECISIONS.md; TB expects golden
  starts + last golden end. (7) Forced failure (+6 % baud) demonstrated:
  `results/week4b_uart_rx_{verilator,icarus}_fail.fst` landed via the
  harness (removed after; gitignored). Waveforms: Verilator FST
  (`dump.fst`, needs trace-capable build + `--trace` — harness now wires
  `-CFLAGS`/`OPT_FAST` defines; root-caused a shared-stale-`verilator.o`
  clobbering + a link rule requiring model+harness trace consistency, fixed
  by making every Verilator build trace-capable), Icarus FST
  (`<toplevel>.fst` under waves); neither emits VCD in this flow.
  (8) Python pinned 3.12: `pyproject` `==3.12.*` (lock re-resolved,
  0 packages changed), AGENTS.md + CI already 3.12, local `.venv` rebuilt
  on 3.12.13; week4b JSONs byte-identical under 3.14 and 3.12.
- Mutations (each reverted; `grep MUTANT` clean): M1 div `>=`→`>` CAUGHT
  by `test_power_directed` (halves); M2 win-start no-reset CAUGHT by
  `test_record_5x` ([S,E) boundary); M3 LUT[0] 9269→9270 CAUGHT by
  `test_lut_sweep_421`; M4 ARM −328→−300 CAUGHT by new
  `test_zc_arm_threshold` (±1-code pin; would have SURVIVED the old suite —
  the test is the fix); M5 NEG bit 5→4 CAUGHT by mixed-sign record tests;
  M6 88-bit→64-bit product SURVIVOR (bound proof above, no test possible
  in-contract — reported, not hidden).
- Verified: `make repro` twice on Python 3.12.13 — 79 passed both times
  (77 + record_agg + lutparam suites), `ruff check` + `ruff format --check`
  clean; `results/week4b_{verilator,icarus}.json` shasum-identical across
  both runs AND the 3.14 run (`8e78b4d1…`, `9a246faa…`); `metrics.json`
  untouched (`b1ea2876…`).
- Open risks: Week 5 divider/sqrt FSM area unknown (Yosys decides);
  `rec_p_avg` exact division is Week 5 descriptor scope; simulation proves
  logic/math/detection only — nothing about physical tamper resistance.

## Week 4b: RTL + cocotb (done)

- Done: `rtl/uart_rx.v` (8N1 byte RX, 16x mid-bit sample, bad-stop drop +
  `framing_error`), `rtl/zc_detect.v` (ARM <-328 codes, trigger first >= 0
  while armed, counter-index boundaries, gap [150, 260] + M [1810, 2230]
  range comparators), `rtl/power_calc.v` (64-bit accumulators, dual P_AVG:
  `p_avg_exact` == `mean_q30_half_away` is the byte-exact descriptor target,
  `p_avg_lut` == `lut_window_mean` via generated 512x24 ROM
  `rtl/recip_lut.vh` from `sim/gen_recip_lut.py`; restoring divider/sqrt,
  exact PF, integer-exact signed energy) — all Verilog-2005, no latches,
  no delays. `tb/golden.py::record_fields` gained the NEG_ENERGY clamp
  (total < 0 → 0 + flags bit 5, P_AVG signed-exact) mirroring
  `edge/attestation.py`, pinned by `tests/test_week4b_golden.py`.
- Verified (SEED=42): `pytest` 77 passed (70 prior + 4 golden-clamp + 3
  dual-sim wrappers), `ruff check` + `ruff format --check` clean.
  zc→power interface/timing exactly per DECISIONS.md (strobes live during
  the detection-sample cycle; [S, E) with E owned by next window; invalid →
  recorded M 0 + all-zero outputs). Directed: every window boundary,
  missing crossing, noise glitch, sag, freq step, M 1809/1810/2230/2231,
  full-scale pos/neg, full-negative-DC clamp, negative-P record (energy 0
  + bit 5 + exact P_AVG, equals `record_fields` and `build_record`
  byte-for-byte), accumulator headroom (2230 full-scale both signs),
  rounding halves (+/-), UART bad-stop/back-to-back/cut-mid-byte/±2 %
  baud/reset-mid-window, 300-frame L0 e2e byte-exact. All 421 M values
  through the LUT, exact. 1,000 seeded vectors under BOTH Verilator 5.048
  and Icarus 13.0: max/mean LSB error 0 on every exact field
  (`results/week4b_verilator.json`, `results/week4b_icarus.json`,
  merged in `results/week4b.json`).
- Measured (actual runs, not assumed): BEHAVIORAL-simulation pipeline
  depth exactly 3 per window (last-sample → `out_valid`) on all 1,000
  vectors × 2 sims — not a hardware timing claim (see Week 4b2:
  divider/sqrt are combinational loops; Week 5 FSMs re-state the count).
  LUT-vs-exact worst relative error 6.61e-05 (0.0066 %) — reproduces the
  D-05 adopted sizing claim. Zero failing vectors, so no failure VCDs exist
  (harness reruns any failure with waves and copies the dump to `results/`).
- Open risks: dividers/sqrt are behavioral loops (sim-exact; Week 5 Yosys
  decides area, fallback alt 3 documented in D-05); simulation proves
  logic/math/detection only — nothing about physical tamper resistance;
  env runs Python 3.14 while AGENTS.md says 3.12 (not chased).

## Week 1: Foundations (done)

- Done: repo root collapsed to `hrot/`; `docs/BLUEPRINT.md` present;
  `docs/DECISIONS.md` + `docs/threat_model.md` written; scaffold per
  blueprint §3 + `sim/`; hello-world cocotb test via Verilator passes.
- Verified: `make test` (1 passed), `make lint` (ruff check + format
  clean), `make repro` (SEED=42) green on 2026-10-06.
- Open risks: icarus-verilog + gtkwave missing (`brew install
  icarus-verilog gtkwave` when needed); Yosys present, unused until Week 5.

## Week 4a3: Width + policy fixes (done)

- Done: record-level width correction (45-bit signed record sums;
  E_rec_max 15,485,165 µWh; full-scale all-5-valid directed test both
  signs); NEG_ENERGY bit-5 flips rejected both directions + per-record
  `neg_energy_clamped` metadata with batch counting (5 of 20);
  threshold-rounding pin test (-328/0 as intended rounding of -5.0/0.0);
  PF exact-match contract for Week 4b.
- Verified: `pytest` 70 passed, `ruff check` + `ruff format --check`
  clean. `make repro` twice identical shasum on `results/metrics.json`.
- Open risks: P_AVG LUT-vs-exact register mapping to be stated in Week 4b.

## Week 4a2: Review fixes (done)

- Done: integer-code detector core (`find_rising_crossings_q15`, arm <
  -328 codes = -5.0049 V, trigger ≥ 0) with volts wrapper proven equal on
  200 seeded signals + exhaustive 4096-code map check; NEG_ENERGY clamp
  (bit 5, `P_AVG` signed-exact, still verifies); old-vs-new rounding
  property test (differ only on exact halves, 1 LSB); measured
  interpolation accuracy (integer 0.1386 % vs fractional 0.1261 % max —
  sensor floor dominates, interpolation stays out of RTL).
- Verified: `pytest` 66 passed, `ruff check` + `ruff format --check`
  clean. Week 3 fixtures/expected values unaffected (no test pinned
  absolute values; 0/24 real windows differed by the rounding change).
  Week 2b `metrics.json` byte-identical after the detector refactor
  (shasum-compared rerun of `run_week2b.py`).
- Open risks: same as Week 4a (sqrt/division RTL unproven until 4b).

## Next (Week 5: Verilog core part 2)

- Hash chain (secworks SHA-256) + HMAC, counter logic, `top.v` wiring the
  Week 4b modules; Yosys LUT/FF/BRAM counts; RTL ledger verifies in
  `verifier.py`.

## Week 4a: Golden power + detector model (done, awaiting review)

## Week 4a: Golden power + detector model (done, awaiting review)

- Done: `tb/golden.py` (standalone UART-parse + fixed-point power +
  descriptor fields; rounding half-away, saturation/widths documented),
  canonical helpers in `sensors/windows.py` (`div_round_half_away`,
  `mean_q30_half_away`, `isqrt_round_half_up`, `energy_uwh_increment`)
  with `edge/attestation.py` aligned to them; D-05 failure-mode directed
  detector tests (dropout, glitch, sag, freq step, exact/near M ends).
- Verified: `pytest` 60 passed (15 new), `ruff check` +
  `ruff format --check` clean. Golden vs float64 over 1,000 seeded
  vectors: P within 0.5 LSB_W, RMS within 1 LSB_V, PF within 2 PF-LSBs.
  No Verilog written or touched in Week 4a (stubs intact).
- Open risks: sqrt/division RTL algorithms specified but unproven until
  Week 4b cocotb; ENERGY integer form bakes in fs = 10 kHz (asserted).

## Week 3: Edge framing + signing (done)

- Done: `edge/framing.py` (D-01 11-byte codec, CRC16-CCITT-FALSE with
  0x29B1 check vector, resynchronising `FrameDecoder`), `sim/provision.py`
  (`--seed 42`, SHA-256 domain-separated keys into gitignored `keys/`),
  `edge/secure_element.py` (HMAC-verify then Ed25519-sign; private key
  never exposed), `edge/attestation.py` (golden FPGA model: descriptor +
  window_hash + HMAC per the Week 3 byte-layout note), `edge/receiver.py`
  (reference: CRC, counter continuity with flag-and-accept reorder,
  hash/HMAC/signature recompute; invalid-flagged windows verify and
  advance the counter).
- Verified (SEED=42): `pytest` 45 passed (22 new), `ruff check` +
  `ruff format --check` clean. Corrupted byte rejected; all 88
  single-bit flips rejected; replay rejected (`rejected-replay`);
  reordered frames strictly rejected (`rejected-reorder`, dropped, counted,
  recorded as gap — strict reject adopted post-review, superseding the
  planning-phase accept-and-flag; replay vs reorder verdicts proven
  distinct); drop reported as gap; wrong-key signature rejected;
  garbage/truncation resynchronised. Scale: e2e plant→AFE→framing→signing
  →receiver with zero false rejects over 10,000 seeded L0 sample frames
  (0 gaps, 0 rejected); mixed valid/invalid sequence with zero false
  rejects over 10,000 seeded ZC sub-windows, i.e. 2,000 signed attestation
  records (1,023 invalid sub-windows accepted as countable records); all
  88 post-signing flips in window_flags‖zc_samples, all flips in
  counter‖window_start‖reserved bytes, and every device_id bit-flip
  rejected. Same-SE-instance fail-then-succeed refusal proven.
- Open risks: receiver HMAC check uses the symmetric key (test harness
  only — production verifier in Week 5 uses the public key per AGENTS.md);
  chain (`prev_hash`) carried but not yet enforced until Week 5.

## Week 2b: Zero-crossing windows, D-05 resolved (done)

- Done: approved mitigation implemented — `sensors/windows.py` golden
  model (rising-ZC detector with ±5 V hysteresis on measured quantized V
  only; N = 10 whole cycles, variable M ∈ [1810, 2230] for the 45–55 Hz
  range; gap check [150, 260]; invalid → NaN + flag, never healed).
  Plant gained grid-distortion options (3rd/5th harmonics, DC offset;
  defaults 0 so Week 2 numbers are untouched).
- Verified (SEED=42, actual run via `sim/run_week2b.py`): `pytest`
  23 passed, `ruff check` + `ruff format --check` clean;
  `results/metrics.json` gains `zc_windows` next to the intact Week 2
  numbers, plus `results/figures/zc_vs_fixed_sweep.png`. Under stress
  (noise + 3 %/1 % harmonics + 2 V DC): fixed bias @ ±0.5 Hz is
  0.66–0.68 % mean / 1.11–1.13 % max; ZC residual is 0.10 % mean /
  0.13–0.14 % max, flat over ±0.5 Hz, 100 % windows valid (~8x better,
  no tuning). Reciprocal-LUT sizing measured: 18 b output costs 0.41 %
  (rejected as material), 24 b costs 0.0066 % (adopted, one BRAM18).
- `docs/DECISIONS.md` D-05 marked resolved: N = 10 default, range/M/gap
  bounds derived above, ledger gains `window_flags` u8 + `zc_samples`
  5×u16 (binary mirror +12 bytes, exact layout Week 5), Week 4 golden
  plan (integer-bound sums = 1-LSB RTL target; variable-M vectors),
  Week 7 sensitivity N ∈ {5, 20} + fixed ablation.
- Open risks: detector proven in simulation only (says nothing about
  physical CT/VT saturation or severe distortion beyond 3 %/1 %);
  dropout/out-of-range handling tested on synthetic gaps, not on a
  real relay event.

## Week 2: Plant + sensors (done)

- Done: `plant/motor.py` (single-phase equivalent-circuit + slip dynamics +
  thermal-coupled R, two-pass), `plant/thermal.py` (first-order I²R state),
  `plant/load_profiles.py` (steady/cyclic/bursty, seeded), `sensors/afe.py`
  (gain/offset/noise/jitter) + `sensors/adc.py` (12-bit, V ±500 Vpk /
  I ±100 Apk) with single `measurement_chain` entry point (no bypass).
- Verified (SEED=42, actual run via `sim/run_week2.py`): `pytest` 17 passed,
  `ruff check` + `ruff format --check` clean; 3 figures in
  `results/figures/`; `results/metrics.json` from the run — per-profile P
  mean/max %: steady 0.101/0.213, cyclic 0.103/0.217, bursty 0.102/0.223,
  all under the spec-derived acceptance bound 1.506 % (noise params fixed
  before the bound was computed, never tuned).
- Frequency drift: sensor-chain error flat across ±0.5 Hz; fixed-window
  bias vs coherent reference grows ~linearly (P mean 0.28 % @ ±0.2 Hz,
  0.70 % mean / 1.1 % max @ ±0.5 Hz). Recorded in `metrics.json` and in
  `docs/DECISIONS.md` D-05 as a known limitation with a zero-crossing
  mitigation proposal (pending approval). True frequency never fed to
  window logic.
- Open risks: motor is an equivalent-circuit approximation (torque agrees
  with slip curve within ~6 % over 6–14 N·m); thermal rise over 5 s runs is
  < 1 K so Week 6 must use longer horizons for thermal-constraint testing.
