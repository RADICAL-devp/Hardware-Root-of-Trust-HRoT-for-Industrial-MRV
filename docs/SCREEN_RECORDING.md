# Screen recording checklist (Week 8)

The 5-minute screen recording is a manual step; this checklist is the
script. Record with any screen-capture tool (QuickTime on macOS).

## Setup (before recording)

1. `make lint && make test-quick` — both green.
2. `make attacks-week7` — regenerates the detection table.
3. `make dashboard` — opens the Streamlit dashboard at http://localhost:8501.
4. Open a terminal in the repo root and `docs/deck.md` (rendered) side by side.

## Script (target ~5 minutes)

- 0:00–0:30  Title slide + honest scope statement ("simulation proves logic,
  math and attack detection; NOT physical tamper resistance").
- 0:30–1:15  Architecture: trust boundaries (SE / FPGA / MPC / verifier).
- 1:15–2:00  Measurement: ZC windows, fixed-point, RTL-vs-golden 1 LSB.
- 2:00–2:45  Savings: Week 6M dashboard tab — 17.20% true, 100/100 coverage.
- 2:45–3:30  Attacks: Week 7 dashboard tab — detection table + 3 honest misses.
- 3:30–4:15  Hardware: Yosys counts, UP5K does not fit, latency 401 cycles.
- 4:15–4:45  Reproduce: `make repro` one command, fixed seed.
- 4:45–5:00  Honest limitations + "no unspoofable claims".

## Capture

- Terminal font size >= 14 pt; dashboard at 100% zoom.
- Record audio narration following the script above.
- Save as `results/screen_recording.mp4` (not committed; gitignored).
- Verify playback end to end before declaring Week 8 done.

## Out of scope for the recording

- Live RTL simulation (too slow); reference the committed `results/` instead.
- Any claim of physical tamper resistance.
