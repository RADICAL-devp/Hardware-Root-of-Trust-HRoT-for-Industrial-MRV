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
- Sensitivity case N = 10 (200 ms) re-run stays available via named constant.

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
