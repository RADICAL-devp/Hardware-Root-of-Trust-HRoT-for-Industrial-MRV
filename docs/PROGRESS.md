# PROGRESS

## Week 1: Foundations (done)

- Done: repo root collapsed to `hrot/`; `docs/BLUEPRINT.md` present;
  `docs/DECISIONS.md` + `docs/threat_model.md` written; scaffold per
  blueprint §3 + `sim/`; hello-world cocotb test via Verilator passes.
- Verified: `make test` (1 passed), `make lint` (ruff check + format
  clean), `make repro` (SEED=42) green on 2026-10-06.
- Open risks: icarus-verilog + gtkwave missing (`brew install
  icarus-verilog gtkwave` when needed); Yosys present, unused until Week 5.

## Next (Week 4b: RTL + cocotb, awaiting week4a review)

- `rtl/uart_rx.v`, `rtl/power_calc.v`, new `rtl/zc_detect.v` against
  `tb/golden.py`; 1,000 seeded vectors (max/mean LSB reported), directed
  cases, all-421-M LUT sweep, VCD-on-failure, Verilator + iverilog.
- Exact RTL cycle latency per window is measured in Week 4b (not stated
  until then — no fabrication).

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
