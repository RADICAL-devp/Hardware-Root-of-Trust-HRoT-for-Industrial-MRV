# Threat Model (STRIDE) — Week 1 Foundations

Honest scope: simulation proves logic, math, and attack detection under
the model below. It does NOT prove physical tamper resistance. No
"unspoofable" / "tamper-proof" claims; tampering is *detected under
threat model X*.

Source of truth: `docs/BLUEPRINT.md` §§1/4, `docs/DECISIONS.md` D-01…D-04.

## Assets

| # | Asset | Location | Integrity mechanism |
|---|-------|----------|---------------------|
| A1 | V/I sample stream + per-sample counter | Sensor/AFE → Edge → FPGA UART | L0 CRC16 + monotonic counter |
| A2 | Window aggregates (Vrms/Irms/P_avg/ENERGY) | FPGA `power_calc` → ledger | SHA-256 hash chain + HMAC |
| A3 | HMAC key (256-bit) | FPGA model + SE model | Never leaves trusted models; sim-only `keys/` gitignored |
| A4 | Ed25519 device private key | Secure element (Python) | Never leaves SE; verifier uses pubkey only |
| A5 | Hash chain (`prev_hash → hash`) + WINDOW_ID | Ledger JSONL | Chain + monotonic counter |
| A6 | Ledger records (incl. `E_saved`, CO₂) | `ledger/` JSONL | HMAC tag + Ed25519 sig per record |
| A7 | Control action `u_k` / failsafe setpoint | MPC → plant | Failsafe bounds check (Week 6) |

## Attackers

| ID | Attacker | Capability | Cannot do |
|----|----------|-----------|-----------|
| T1 | Network / MITM (Dolev-Yao) | Read, drop, reorder, replay, inject, bit-flip on UART + ledger transport | Forge HMAC/SIG, extract keys |
| T2 | Compromised MPC | Wasteful `u_k`, omit/duplicate control msgs, attack ledger transport as T1 | Mint valid records (no keys) |
| T3 | Ledger editor | Edit/delete/reorder JSONL rows offline | Produce valid chain+HMAC+SIG for forged rows |
| T4 | Sensor spoofer (pre-SE) | Inject false analog V/I before the AFE | Beat the SE-signed window without detection *once SE is in path* (Week 7 measures both with/without SE) |

## In-scope attacks (each run 100+ times with fresh seeds in Week 7)

| Attack | STRIDE | Target | Detection (expected) | Miss condition (honest) |
|--------|--------|--------|----------------------|-------------------------|
| Replay (valid old window/frame) | Tampering / Repudiation | A1/A5 | Rejected: WINDOW_ID/counter already seen, chain fork | Miss if window cache unbounded eviction — sized in Week 5 |
| Spoof V/I *with* SE in path | Spoofing / Tampering | A1 | Detected: attacker lacks HMAC/Ed keys; window_hash mismatch | Miss if spoof occurs pre-AFE analog (physics, not crypto) — reported as such |
| Spoof V/I *without* SE (ablation) | Spoofing | A1 | Missed by design in ablation run | Demonstrates why SE is required |
| Bit-flip (UART/transport) | Tampering | A1/A2 | Rejected: CRC16 (L0) / HMAC (L1) fail | Miss only if flip lands in unprotected padding — none by construction |
| Drop (packets/windows) | Denial of service | A1/A5 | Detected: counter gap + chain discontinuity; flagged, not silently healed | Sustained >50% drop degrades availability (see NOT defended) |
| Ledger edit/delete/reorder | Tampering / Repudiation | A5/A6 | Detected: `verifier.py` chain+HMAC+SIG fail, counter gap | Miss if verifier is given attacker's pubkey (trust-anchor swap — out of scope) |
| Compromised MPC (wasteful control) | Tampering / Elevation | A7/A6 | Detected as *negative savings*, not as forged savings: ledger stays valid, `E_saved` drops | Cannot forge savings records; physical waste itself is not prevented |

## NOT defended against (explicit)

- Physical probing / side channels / key extraction from real SE/FPGA silicon.
- HMAC-key extraction from a running FPGA (Week 5c amendment): the key
  lives in an FPGA register at runtime; Week 5 proves key USE (bit-exact
  HMAC under the test vectors) NOT key PROTECTION — extraction is out of
  scope, same as SE-silicon probing above.
- Pre-AFE analog spoofing (physics beats crypto; only *detected* via baseline residuals, Week 6).
- Trust-anchor swap (attacker convinces verifier to use attacker's pubkey).
- Availability under sustained drop/jam (detection ≠ delivery).
- Grid emission-factor fraud (wrong factor in, wrong CO₂ out).
- Three-phase imbalance attacks (single-phase first per AGENTS.md; Phase-2 work).
- Nonce reuse by a mis-implemented SE (forbidden; monotonic counter enforced in `framing.py` Week 3).
