---
marp: true
theme: default
paginate: true
---

# HRoT Carbon Verification Module

**Measured, controlled, cryptographically attested energy savings — simulation**

Honest scope: simulation proves logic, math and attack detection.
It does NOT prove physical tamper resistance.

---

# 1. Problem

- Energy savings from motor retrofits are claimed but rarely measured honestly.
- Baselines, sensors and controllers can be gamed or tampered with.
- Carbon accounting must be auditable end to end.

Goal: a verification module that measures, controls, attests and detects tampering.

---

# 2. Architecture (trust boundaries)

- **Secure element (Python):** holds Ed25519 key, signs at the source.
- **FPGA core (Verilog):** power calc, monotonic counter, SHA-256 hash chain, HMAC.
- **MPC:** UNTRUSTED — can waste energy, cannot forge savings.
- **Verifier:** public key only.

Keys never leave the trusted models.

---

# 3. Measurement (Weeks 2–5)

- 10 kHz sampling, 12-bit ADC + noise from day one.
- Zero-crossing windows (10 cycles): bias 0.66–0.68% → 0.10%.
- Fixed-point Q15/Q16; RTL matches golden model within 1 LSB.
- L0 CRC16, L1 attestation record, hash chain, HMAC, Ed25519 signature.

---

# 4. Control (Week 6)

- Three-phase dq induction machine (quasi-static 4-state reduction).
- Flux/frequency-dependent core loss (amendment 10).
- MPC with failsafe; derated capability constraint.
- Savings acceptance replaced by **interval coverage** over N seeds.

---

# 5. Savings result (Week 6M)

- Legacy (constant speed + throttle) vs retrofit (VSD), equal service.
- True savings: **17.20%** of legacy energy.
- Estimated: 17.23% ± 1.42% (SE 1.76%).
- Coverage: **100/100** (gate ≥ 90/100, PASS).
- CI half-width: 4.44% (cap 7.79%, PASS); 100% of CIs exclude zero.

---

# 6. Attack campaign (Week 7)

| Attack | Detected | Verdict |
|---|---|---|
| Replay | 128/128 | bad-replay |
| Spoof V/I with SE | 128/128 | bad-signature |
| Bit flip | 128/128 | bad-descriptor |
| Drop | 128/128 | bad-chain |
| Ledger edit/delete | 128/128 | bad-descriptor / bad-chain |
| Compromised MPC | 128/128 | negative savings |
| False-reject rate | 0.0000 | — |

---

# 7. Honest misses

- **Spoof V/I without SE (ablation):** by design the attacker is the signer.
  Demonstrates why the SE is required.
- **Pre-AFE sample spoof:** physics, not crypto. Keyless verifier has no
  samples → needs a physical cross-check.
- **Compromised MPC waste:** ledger stays valid; detected only as negative
  savings at the energy layer.

---

# 8. Hardware (Week 5)

- Yosys: 15,851 LUT4, 10,979 FF, 0 BRAM.
- UP5K: does NOT fit (447%).
- Latency: 401 cycles per attestation window.
- Latches inferred: 0.

---

# 9. Reproduce

```sh
make repro          # SEED=42, full suite (deterministic)
make lint           # ruff check + format
make test-quick     # Week 6/7/8 Python suites
make attacks-week7  # attack campaign (5.5 s)
make dashboard      # Streamlit evidence dashboard
```

All random seeds fixed. `make repro` is bit-reproducible.

---

# 10. Honest limitations

- Simulation proves logic/math/attack detection, NOT physical tamper resistance.
- Week 5 hardware limits: UP5K does not fit; Fmax unmeasured.
- Week 6 disposition ACCEPTED under re-scoped acceptance (interval coverage;
  torque option B). Compromised-MPC waste is detected, not prevented.
- Reported attacks we miss (see slide 7).

**No "unspoofable" / "tamper-proof" claims.**
