# HRoT OpenCode Phased Execution Prompts

## Phase 1 (Week 1): Kickoff (Run in Plan Mode)
Read @docs/BLUEPRINT.md and @AGENTS.md fully.

Task: Week 1 only (Foundations), plus resolving design gaps before any code.

1. Write docs/DECISIONS.md proposing answers to these blueprint gaps, with a one-line rationale each:
   a. The telemetry frame ends in "[...]", so define the full byte layout, endianness and CRC16 variant.
   b. Signing granularity: Ed25519 on every 10 kHz sample frame is unrealistic. Propose signing per attestation window (e.g. N whole 50 Hz cycles of samples) and say what the FPGA hashes vs. what the secure element signs.
   c. How the HMAC key and the Ed25519 key are provisioned in simulation, and what a "compromised MPC" can and cannot do.
   d. Window length, sample rate, and fixed-point formats for V, I, P.
2. Scaffold the repo exactly as in blueprint section 3 (empty modules with docstrings are fine), pyproject.toml via uv, a Makefile (`make test`, `make lint`, `make repro`), and GitHub Actions running ruff + pytest.
3. Write docs/threat_model.md as a STRIDE table: assets, attackers, in-scope attacks (replay, spoof with/without secure element, bit-flip, drop, ledger edit/delete, compromised MPC), and an explicit "NOT defended against" section.
4. Add a hello-world cocotb test (tb/test_hello.py) that drives a trivial Verilog module via Verilator, and make pytest run it.

Done when: `make test` and `make lint` pass, and docs/DECISIONS.md and docs/threat_model.md exist.

First, show me your plan and the proposed DECISIONS.md content. Do not write code until I approve.

---

## Later Phases Template (Run in Build Mode)
Implement Week {N} from section 5 of @docs/BLUEPRINT.md, following @AGENTS.md and @docs/DECISIONS.md.

1. Restate the "done when" criterion for this week as a runnable check.
2. Write the failing test for it first.
3. Implement until it passes. Show me the real test output.
4. Update docs/PROGRESS.md, commit as `week{N}: ...`, and stop.

### Phase-Specific Additions:
- **Week 4:** "Generate 1,000 random vectors from the Python golden model, drive them through the RTL with cocotb, and report max LSB error. Include a waveform (VCD) for one failing case if any."
- **Week 5:** "Run Yosys synth and put LUT/FF/BRAM counts in results/metrics.json. Verify the RTL-produced ledger with ledger/verifier.py."
- **Week 7:** "Run each attack 100+ times with different seeds. Output a detection table (attack × detected/missed × rate) and a short written explanation for every miss. Also measure the false-reject rate under noise and dropped frames."
