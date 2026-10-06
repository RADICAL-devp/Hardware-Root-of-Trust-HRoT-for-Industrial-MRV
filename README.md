# HRoT Carbon Verification Module

Simulation proving that energy savings from a legacy motor are measured,
controlled and cryptographically attested, and that tampering is detected.

Honest scope: simulation proves logic, math and attack detection. It does
NOT prove physical tamper resistance.

## Reproduce

```sh
make repro   # fixed seed (SEED=42), hello-world cocotb test via Verilator
make test    # pytest (tb/ + tests/)
make lint    # ruff check + format check
```

See `docs/BLUEPRINT.md`, `docs/DECISIONS.md`, `docs/threat_model.md`.
