# PROGRESS

## Week 1: Foundations (done)

- Done: repo root collapsed to `hrot/`; `docs/BLUEPRINT.md` present;
  `docs/DECISIONS.md` + `docs/threat_model.md` written; scaffold per
  blueprint §3 + `sim/`; hello-world cocotb test via Verilator passes.
- Verified: `make test` (1 passed), `make lint` (ruff check + format
  clean), `make repro` (SEED=42) green on 2026-10-06.
- Open risks: icarus-verilog + gtkwave missing (`brew install
  icarus-verilog gtkwave` when needed); Yosys present, unused until Week 5.

## Next (Week 3: Edge framing + signing)

- Frame encoder with CRC16-CCITT-FALSE and monotonic counter (DECISIONS.md
  D-01/D-02); simulated secure element holds Ed25519 key, signs per window.
- Python reference receiver rejects corrupted/replayed frames.
- Ledger record gains `window_flags` + `zc_samples` per D-05 (resolved);
  attestation stays 1 Hz over 5 ZC sub-windows; L0 sample frames unchanged.

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
