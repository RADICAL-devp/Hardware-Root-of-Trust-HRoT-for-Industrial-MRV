# HRoT Carbon Verification Module

Simulation proving that energy savings from a legacy motor are measured,
controlled and cryptographically attested, and that tampering is detected.

Honest scope: simulation proves logic, math and attack detection under the
stated threat model. It does NOT prove physical tamper resistance. No
"unspoofable" / "tamper-proof" claims; tampering is *detected under threat
model X*.

## One-command reproduction

```sh
make repro        # SEED=42, full pytest suite (deterministic)
```

Faster subsets:

```sh
make lint         # ruff check + format check
make test         # pytest, non-slow nodes
make test-quick   # Week 6/7/8 Python suites only (~1.5 min)
make test-full    # everything, including slow RTL sims (~1.5 h)
make attacks-week7  # Week 7 attack campaign (5.5 s)
make dashboard    # Streamlit evidence dashboard
```

## Week 8 deliverables

- `dashboard/app.py` — Streamlit dashboard on `results/metrics.json`
  (Savings / Attacks / Physics / Gates tabs). Run: `make dashboard`.
- `docs/deck.md` — 10-slide deck (Marp-compatible Markdown).
- `docs/SCREEN_RECORDING.md` — checklist for the 5-minute screen recording.
- `results/metrics.json` — single source of truth for all reported numbers.
- `README.md` — this file.

## Reproduce from a clean checkout

```sh
uv sync                       # install deps (Python 3.12)
make repro                    # SEED=42, full suite
```

All random seeds are fixed; `make repro` is bit-reproducible.

## Documentation

- `docs/BLUEPRINT.md` — source of truth (roadmap, metrics).
- `docs/DECISIONS.md` — owner decisions, amendments, findings.
- `docs/PROGRESS.md` — per-phase done / next / open risks.
- `docs/threat_model.md` — STRIDE model, defended vs not-defended.
- `docs/deck.md` — 10-slide deck.
- `docs/SCREEN_RECORDING.md` — recording checklist.

## Honest limitations

- Simulation only: proves logic, math and attack detection. Does NOT prove
  physical tamper resistance.
- Week 5 hardware limitations preserved: UP5K does not fit (ICESTORM_LC 447%);
  Fmax unmeasured; 101-cycle chain-tail variation unresolved.
- Week 6 disposition: ACCEPTED under the owner's re-scoped acceptance
  (interval coverage; torque option B). The compromised-MPC physical waste is
  detected only as negative savings, not prevented.
- Reported attacks we miss: spoof V/I without the secure element (ablation,
  by design), pre-AFE sample spoof (keyless verifier has no samples), and
  compromised-MPC waste at the energy layer.
