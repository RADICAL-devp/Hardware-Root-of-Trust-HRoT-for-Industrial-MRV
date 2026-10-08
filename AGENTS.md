# HRoT Carbon Verification Module

Source of truth: docs/BLUEPRINT.md. Read the relevant section before starting any phase.

## Honest scope
- Simulation proves logic, math and attack detection. It does NOT prove physical tamper resistance.
- Never write "unspoofable" or "tamper-proof". Write "detected under threat model X".
- Never fabricate results. metrics.json and any reported number must come from an actual run. Report attacks we miss, and why.

## Environment
- macOS, Python 3.12, uv only (uv add / uv run). Never pip install.
- HDL: Verilator 5 + cocotb for tests, Icarus + GTKWave for quick checks, Yosys for LUT/FF counts.
- If a tool is missing, tell me the exact brew command. Do not silently work around it or mock it.
- Fixed random seeds everywhere. Everything must be reproducible with a single `make repro`.

## Architecture rules
- Trust boundaries: secure element (Python) holds the Ed25519 key and signs at the source; FPGA core (Verilog) does power calc, monotonic counter, SHA-256 hash chain, HMAC; MPC is UNTRUSTED; verifier uses only the public key.
- Python golden model FIRST, then RTL must match it within 1 LSB (fixed-point Q15/Q16).
- Never put MPC in Verilog. Never reuse nonces. Never hardcode the baseline.
- Sensors always have noise and 12-bit quantization from day one.
- Finish single-phase end to end before touching three-phase.
- Third-party RTL (secworks): clone into rtl/third_party/, pin the commit hash, note the license.

## Workflow
- Test first: write the failing test that encodes the phase's "done when" criterion, then implement.
- Run pytest and ruff before declaring anything done. Show me the actual output.
- No phase is done until `make test-full` and `make repro` pass; every report
  names the target it ran (`make test` is the fast subset, `make test-full`
  is everything, `make repro` is the full suite at SEED=42).
- Commit per phase: `weekN: <summary>`. Update docs/PROGRESS.md (done, next, open risks).
- Do ONE phase, then stop and report. Do not start the next phase.
