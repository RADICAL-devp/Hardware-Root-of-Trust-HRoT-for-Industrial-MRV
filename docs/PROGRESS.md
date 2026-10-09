# PROGRESS

## Week 5d2b: review follow-ups (gates + commit, then stop)

- Bit 7 `HASH_OVF` + invalidate verified present (`top.v:52`, `:776-784`:
  flags `&0x60|0x80`, p_avg 0, energy = previous cumulative, consumed at
  assembly); production path pinned at unit (`test_skid_overflow17`).
  E2e flag-flip attempted two ways, both honestly closed: VPI deposits do
  not stick on this Verilator setup (landing readback 0); continuous-flow
  trigger computed reachable (~17.5) but measured unreachable (peak ~15,
  no fire) — new `test_top_continuous_clean` pins the harshest flow exact
  (r0/r1 bit-exact, sc[3]==0). 5d3 verifier rule (REJECT bit-7 records,
  never into baseline/aggregation) in DECISIONS.md 5d2 section.
- Every quoted figure now pinned by exact test asserts both sims
  (side files are residue): counter rec0-3 (1019/617, 1054/652, 1046/644,
  1054/652), small/device7 (1011/609), full (1120/718), peak == 8,
  402-diff on every record, >=10x margin. Decomposition: base 617 (rec0
  genesis-valid), validity −8, prev +27, jitter +0/+8, full-shape +101
  (mechanism OPEN — bounded, sims-agreeing, stated as such; do not quote
  a mechanism). `meas_*` append per record now (was truncate-last).
- Counter path extended to 4 back-to-back records (5200-sample stream,
  20 windows): chain→HMAC→chain three times, every record hash/HMAC
  exact with prev chaining, both sims. sc_counter asserted == k in
  counter/sidecar/e2e (carries-its-record proven).
- Yosys 0.66 key-independence measured: full-tree `proc;opt;stat`
  3899 cells / 4184 wires IDENTICAL for key 0 vs all-1 (temp wrappers
  deleted). LUT/FF techmap counts stay 5d4.
- Review-item status: (1) CLK_PER_BIT=8 done (B1 + `top.v:120`, e2e at
  nominal every gate); (2) key port-only done + counts measured;
  (3) whitelist + `-chparam`/`-P` tests exist, halt unit via
  `parameters=` (`test_counter_halt`) + e2e (`halted_out==0`,
  increments), elaborated dump is 5d4 (stated, not built); (5) e2e_full
  slow-marked 880/frame byte-exact both sims; (7) M-T1..M-T8 table stands
  (rejected-hashed M-T7, skid-full M-T8, mux-early M-T5, E-current M-T6).
- Gate: `make lint` clean; `make test-full` 253 passed (251 + 2 new
  continuous_clean nodes, reconciled), zero failures; `make repro`
  (SEED=42) 253 passed. Targets named per rule.

## Week 5d2: top.v integration (built — gates + commit, then stop for review)

- Done: `rtl/top.v` end-to-end wiring (all 5d1 units + `rtl/skid_buf.v`
  new) per B1–B3 + amendments; 12 top cases + B3 repo guard, all green
  both sims (40 passed `test_week5d_top` + `test_week5d_chain`, 13:30).
  DECISIONS.md 5d2 section: budget-vs-measured table (post-close
  644/718 vs 800 budget; winend5 1046/1120 with the 402 = 401+1 check;
  skid peak 8), mutants M-T1..M-T8 (all killed, reverted, `grep MUTANT`
  clean; survivors: none).
- Build fixes worth knowing: phase-gated chain start (same-edge
  HMAC collision wedged job≥2 in PREV — the mid-build pending
  failures); push/tap registered coherence; wstart staging;
  `first_start_done` spans records. TB-side (DUT was correct): sidecar
  dirty confined to `< 1000` (phase-A tail belongs to record 1);
  repo-guard regexes (DEVICE_ID + module-header span; negative
  controls re-proven).
- Next: commit `week5d2`, then stop. 5d3 verifier owns: NV counter, second
  genesis flag, skid-overflow verifier rule; 5d4 owns the synth
  elaborated-parameter dump.
- Gate: `make lint` clean; `make test-full` 251 passed (223 + 24 top
  nodes + 4 repo-guard, reconciled), zero failures; `make repro`
  (SEED=42) 251 passed. Targets named per rule.
- Open risks: 1M SIGSEGV (~1/300) still open (no new evidence);
  `meas_open` truncates (only last record's line survives — rec2
  reported); three-phase not started; key protection out of scope.

## Week 5d1b: derived column + tomb/473 pins + frozen guard + faulthandler (done — commit, then 5d2 plan)

- 5d1 table now carries an explicit derived column (blocks×67 + OH):
  205/203/205/404/206/8578 all match exactly. Overhead rule OH = 69 +
  2 (block0 in DESC) + 1 (empty SAMP_END); the 203/205/206 spread is
  structural (handshake exposed vs overlapped vs SAMP_END), SAMP +
  DESC-to-64 always sums to 32. Tomb total pinned 8578 (67 + 126×67 +
  69) with all-127-gaps-67 — feed ≥ drain proves no starvation at any
  length, so a stall is a bug. Universal tail last-accept → done == 69
  pinned in `run_record` for EVERY clean job (TB-free path).
- 473 breakdown is a permanent pin: `test_hmac_skid` asserts per 68 B
  job — accepts gaps [67, 67, 136, 67], 2 wrap-inits, 5 core dones —
  both sims (relative pins, monitor-phasing robust).
- Named (all pre-existing, cited in DECISIONS.md): `test_counter_halt`
  (CNT_INIT MAX−2 → halt sticky, never wraps), `test_chain_rejected_frame`
  (accepted-only == same record sans frame, both directions),
  `test_counter_mono` rejected-close cases (M-C3 catcher).
- Repo guard `tests/test_in_sim_no_frozen.py` (closure walk over all
  `tb/*.py`, alias-aware, all 3 decorator forms probe-proven): 4th
  conversion `sensors/adc.ADCParams` after an import-time NameError hit
  a 5d1 sim startup. Host-side params + non-frozen dataclasses stay,
  justified in DECISIONS.md.
- 1M crash STAYS OPEN: `PYTHONFAULTHANDLER=1` in all nine sim wrappers
  (retry policy unchanged); waveform route replaced (VCD saves kept
  for assertion context; crash forensics = faulthandler + crash
  reports; FST viewer dropped). Versions: macOS 27.0 (26A428),
  Mac14,15 arm64, venv 3.12.13, sim-reported 3.12.10 string (skew noted
  verbatim, open). 30× faulthandler 1M mini-campaign: 30/30 pass,
  0 crashes (rate-uninformative at ~1/300 — validates the wrapper
  change, no faulthandler trace yet to inspect).
- Gate: `make lint` clean; `make test-full` first attempt 217 passed +
  1 startup-death (`test_hash_overflow-verilator`, pytest-import
  NameError, zero verdict — the event that forced the retry-policy
  extension above; node recovered green immediately); rerun for the
  record 223 passed, zero first-attempt failures, zero FLAKY lines
  (217 + 1 frozen-guard + 5 new retry-matcher cases = 223, reconciled);
  `make repro` (SEED=42) 223 passed, zero first-attempt failures.
  Targets named per rule.

## Week 5d1: chain + counter (done — commit, then stop for 5d2 review)

- Files (this commit): `rtl/chain_ctrl.v`, `rtl/win_counter.v` (new);
  `tb/chain_wrap_pair.v` (test-only harness), `tb/w5d_chain_cocotb.py`,
  `tb/test_week5d_chain.py` (new; 8 cases × 2 sims = 16 nodes);
  `.github/workflows/ci.yml` (+ `test_week5d_chain.py` matrix line);
  DECISIONS.md (5d1 section: design, cycle table, mutants M-C1..M-C5,
  amendment decisions 1–7).
- Test-first (honest red/green): tests written before RTL — initial red
  was missing modules. First green run then caught THREE test-side bugs
  (red doing its job, DUT exonerated each time): a dropped
  `msg += desc` line in the tomb layout (localized by byte-diffing the
  sim message vs host: sim prefix == host prefix, desc missing), and
  genesis/prev mismatches in empty/rejected (DUT feeds zeros under
  genesis — tests now use zeros; the chained-prev path is covered by
  genesis_link recs 1–2 and the strengthened rejected-frame record B).
  No RTL change was needed for any of them.
- Harness (4th dataclass, same mechanism): `test_chain_empty_samples`
  flaked once between green runs with the import-time
  `__dataclass_builtins_object__` NameError (sim startup,
  `exec_module` — first observed AT IMPORT rather than at
  construction). Converted `sensors/adc.ADCParams` → NamedTuple (last
  frozen dataclass in the in-sim import closure: constructed at
  `edge/framing` import, itself imported by frame/hole TBs); 30×
  reruns of the flaked node clean. Sweep disposition (DECISIONS.md):
  in-sim frozen → converted (4 total); host-side params + all
  non-frozen dataclasses → left with mechanism-specific justification.
- Vectors (all exact, both sims): 3-record genesis chain (linkage +
  205/203/205 pins), tomb-slot record (== `build_record` hash, 127
  blocks), rejected-frame record (chained prev + 11 B hole,
  accepted-only digest, 404 pin), empty record (206 pin), stream
  violation (error, no done, clean recovery), counter 1000-mono +
  rejected-close gate, halt at MAX (CNT_INIT MAX−2) + sticky, volatile
  reset. Structural pins: all spacings 67, last-accept→FINISH 68.
- Mutants M-C1..M-C5 (each reverted; `grep MUTANT` clean; survivors:
  none): order-swap → genesis_link rec0; prev-dropped → rec1;
  valid-gate removed → mono rejected-close; halt removed → halt
  (wraps); stale-prev → rec1. Table in DECISIONS.md.
- Cycle derivation (TB convention, both sims identical): stream bytes
  + blocks×67 + first-accept latency + 68 tail + 2-edge desc handshake;
  table with derived-vs-measured columns in DECISIONS.md (205/203/205,
  404, 206; tomb unpinned-total by design — may stall on backpressure).
- Gate: `make lint` clean; `make test-full` 217 passed (201 + 16 new);
  `make repro` (SEED=42) 217 passed. Targets named per rule. STOP —
  awaiting review before 5d2.

## Week 5c4: 1M flake campaign + dataclass sweep + rate/138 docs (done — commit, then 5d1)

- `test_hash_1M-verilator` campaign (third flake): 100× ALONE =
  99 pass / 1 fail; 100× UNDER LOAD (fast suite alongside: 200 passed)
  = 100/100, zero crashes. The single alone-failure (iter 23) is a
  TRIPLE SIGSEGV — first attempt + retry + waves rerun all died with
  `terminated with error -11`, zero assertion content. macOS crash
  reports root-cause it: EXC_BAD_ACCESS in CPython IMPORT machinery
  (`PyImport_ImportModuleLevelObject` ← `__import__` ← exec) ~170 ms
  after launch (startup imports, test body never ran) — INFRA, same
  family as week5b2's tracked SIGSEGVs, clustered by transient host
  state. NOT the DUT (no verdict; same binary passes before/after);
  NOT the dataclass NameError (1M path constructs none). Retry policy
  behaved as designed (double-crash fails hard, counted FLAKY, never
  green). Observed rate ≈ 1/300 node-runs on the heaviest-VPI test vs
  ~0.1%/node background.
- Dataclass sweep (`tb/ sim/ edge/ ledger/`): converted the three
  in-sim frozen dataclasses → NamedTuple (`provision.KeyMaterial`
  200×/run hot path, `framing.Frame` via in-sim `decode_frame`,
  `windows.ZCParams` once-per-sim at import with properties kept);
  LEFT host-side params (once-per-run, 400+ clean passes) and all
  non-frozen dataclasses (no builtins closure — mechanism-specific).
  Smoke green (`random200`, `fuzz_corrupt`, 30 host tests).
- gtkwave: uninstallable (`brew install gtkwave` → formula
  deprecated/disabled 2025-10-29). No local FST inspector; forensics
  via tracebacks + crash reports + `meas_*.txt` (prints vanish in-sim).
- Docs: line-rate label corrected (TB "line" 1 B/120 cyc = 100 kB/s
  @12 MHz runs 1.25× hotter than the true 80 kB/s = 1 B/150 cyc hash
  rate — conservative; values unchanged) and the 473 planning-table
  `330 + control` corrected to `335 + control`; the 138 overhead broken
  down MEASURED (one-off instrumented 68 B job, tag exact; probe
  deleted after): INIT 1 + IN_PAD 64 + IN_MSG 68 + IN_DV 136 + INIT2 2
  + OUT_PAD 64 + OUT_MSG 32 + OUT_DV 105 + FINISH 1 = 473 exact; accept
  gaps 67, 67, 136, 67 (three core-bound, one structural inter-pass:
  68 tail + 2 init + 64 pad + 2 handoff).
- Tables (BOTH columns, measured vs derived): core accept→digest_valid
  66/66, re-arm ≤2/1, spacing 67/67, drain 1/1, stress peak fill/221,
  line peak (corrected pace) near-empty/1, HMAC c1–c5 403/403, c6
  673/673, c7 807/807, attestation 473/473, window 473/473 peak 86.
- Gate: `make lint` clean; `make test-full` 201 passed; `make repro`
  (SEED=42) 201 passed. Targets named per rule.

## Week 5c3: tables + 67/66 + window + skid + p2r flake root-cause (done — commit, then stop)

- Files (this commit): `docs/DECISIONS.md` (unified both-column
  tables, 67 = 66 + 1 re-arm, HMAC 5×67 + overhead, 473-vs-~350
  reconciliation, skid ~10 clarification), `tb/w5_hash_cocotb.py`
  (stale ~350 comment → 473 normative), `tb/w5_hmac_cocotb.py`
  (`skid_put` rejects-before-append: fail-loud, no silent growth),
  `tb/golden.py` (frozen dataclasses → NamedTuple: harness flake fix,
  same fields/values/immutability), `tests/test_week5c_skid_overflow.py`
  (new: 17-deep bound test, 2 nodes).
- Tables re-pasted with BOTH derived and measured columns in
  DECISIONS.md (wrap + hmac sections) and in the turn report: core
  accept→digest_valid 66/66, re-arm ≤2/1, block spacing 67/67, final
  drain 1/1, stress peak fills/221, line peak near-empty/1, HMAC
  c1–c5 403/403 (4×67+135), c6 673/673 (7×67+204), c7 807/807
  (9×67+204), attestation 473/473 (5×67+138), HMAC window 473/473
  (peak 86). All pins hold on both sims (asserted in-test).
- 67 vs 66 RECONCILED: not a discrepancy — 66 is core
  accept→digest_valid, 67 = 66 + 1 re-arm cycle; every block spacing
  and HMAC core-cost term uses 67 deliberately. Attestation HMAC
  total restated as 5 × 67 + control overhead = 335 + 138 = 473.
- HMAC window RECONCILED: CURRENT is 473 cycles (peak 86) — measured
  at the hmac level on the 68 B attestation shape, both sims,
  pinned in `test_hmac_overlap` + `test_hmac_skid`. The "~350" in
  `test_hash_overlap` was an early CORE-ONLY estimate (5×67 = 335 ≈
  350) before stream/control were measured; SUPERSEDED (missing
  138 cycles). The skid derivation cites 473 (correct). Wrap-level
  400-cycle withhold proves withhold-correctness only, not the length.
- Skid depth CLARIFIED: old "~10 B realistic" counted the TEST
  schedule (8 B burst + 3 line ticks = 11 B in-window), strictly more
  pressure than line rate — not a second sample. Line-rate realistic
  is ONE sample tick = 8 B (473 < 1200-cycle period, two ticks can
  never co-land); 16 B is the absolute margin case (2 samples).
  Depth 16 = 2× line-rate + covers the 11 B test burst and the 16 B
  back-to-back margin job (peak == 16 pinned, load-bearing). New
  17-deep test (`tests/test_week5c_skid_overflow.py`): 16 queue fine,
  17th raises `AssertionError` (rejected-before-append, never
  silently dropped or grown); the 16 drain in order, digest-checked.
  DUT-level overflow-signal proof stays in `test_hash_overflow` /
  `test_hash_sealed_force` / `test_hmac_overflow_abort` (sticky
  `overflow_out`, intact digest).
- Flake campaign (`test_p2r_clear_atomic-verilator`): REPRODUCED then
  ROOT-CAUSED to the harness (NOT the DUT) and FIXED. Pre-fix: 2/100
  alone + 2/100 in a second 100-sweep (each failing first-attempt at
  ~86 µs sim time, passing the same-binary waves-rerun at ~150 µs).
  Captured traceback (`-s` log): `NameError: cannot access free
  variable '__dataclass_builtins_object__'` at `tb/golden.py:278`
  (`DescriptorFields(...)` construction in `record_fields`) — a
  Python frozen-dataclass machinery flake, mid-test, before any DUT
  assert. Same compiled binary + deterministic vectors passing on
  rerun exonerates the DUT. Fix: `tb/golden.py` `L0Sample` /
  `WindowPower` / `DescriptorFields` converted `@dataclass(frozen=True)`
  → `NamedTuple` (same fields, values, immutability; C-level
  constructor, no exec'd `__init__` builtins closure). Post-fix: 100/100
  alone + 100/100 with the fast suite running in parallel (background
  `pytest -m "not slow"`: 200 passed), zero `NameError`s. A
  pass-on-retry was counted FLAKY throughout, never green.
- VCD note: no FST tools locally (`brew install gtkwave` provides
  `fst2vcd`/`fstinfo` — not installed per AGENTS.md, so no waveform
  inspection was possible); the preserved
  `results/week5b_power_record_chain_verilator_test_p2r_clear_atomic_fail.fst`
  (gitignored, local only) is from the PASSING waves-rerun, so it
  cannot show the failure — diagnosis came from the captured
  traceback instead. Same applies to the 1M FST below.
- Unexplained failures, listed together (honest record): (1) week5a
  Verilator single (first-attempt log swallowed by the old wrapper;
  unreproduced over 1200+ node-passes; the same frozen-dataclass
  construction existed in its golden path `parse_l0_stream →
  L0Sample`, now also fixed by the NamedTuple change, but the link is
  unconfirmed); (2) `p2r_clear_atomic` week5c2 single — NOW EXPLAINED
  and FIXED above (harness `NameError`, closed); (3, NEW this turn)
  `test_hash_1M-verilator` single in the first `test-full` (1 failed,
  200 passed; no first-attempt text — tail-only log; 20/20 isolated
  reruns + full-suite rerun clean; preserved
  `results/week5c_sha256_wrap_verilator_test_hash_1M_fail.fst`, 16 MB,
  is from the passing rerun). The 1M path uses no golden dataclasses
  and this commit touches no hash RTL/TB logic (comment only), so no
  mechanism links it to this diff. (1) and (3) remain open as single
  events; (2) is closed.
- CI run #23 URL (recorded this turn; conclusion was already in
  week5c2): https://github.com/RADICAL-devp/Hardware-Root-of-Trust-HRoT-for-Industrial-MRV/actions/runs/37851823534
  (tip 4bcb569, SUCCESS, 11/11 jobs, zero retries).
- Gate: `make lint` clean; `make test-full` rerun 201 passed in 5:45
  (first attempt 200 passed + the 1M flake above; rerun 201/201 — the
  flake is above counted FLAKY, not green); `make repro` (SEED=42) 201
  passed in 5:45, zero first-attempt failures. Node count 199 → 201
  (+2 new skid-overflow nodes, fast, no sim). Targets named per rule.

## Week 5c2: review follow-ups (a)–(f) (done — commit, then stop)

- (a) Tables re-pasted with numbers in the turn report; sources of
  truth: measured-vs-derived in DECISIONS.md (wrap + hmac sections),
  mutation table in the part-3 section below. All pins hold on both
  sims (asserted in-test).
- (b) CI Report prints `tool iverilog / verilator / python3-runner`
  notices into the job summary (validated locally on synthetic logs);
  run #23 (tip 4bcb569) SUCCESS, 11/11 jobs, zero retries (no
  FLAKY/retries annotations on any check-run).
- (c) `test_hash_abort_restart` (both sims): abort mid-FILL + abort in
  the sticky window, restarts exact with `dones == accepts` (4 and 2)
  and one new digest each; fails under M-H6 (second abort-path
  catcher); invariant written into DECISIONS.md wrap section.
- (d) `test_hmac_keylen_boundary` (both sims): keys 63/64/65/66/128/
  131/160 vs oracle + per-pass buckets; kills M-H8 independently
  (65+ mismatch; proven by mutant rerun, then reverted).
- (e) Gate below reruns everything on the final tree; pinned values
  are asserted in-test (a pass IS the tolerance check — empty
  tolerance diff); `git diff --stat tb/` shows additions only.
- (f) Skid-16 derivation (sample-atomicity: ≤ 1 tick + partials per
  473-cycle window, depth 16 = tight power-of-2) in DECISIONS.md;
  `test_hmac_skid` (both sims): greedy source + physical job
  (8-burst + ticks, window 473) + margin job (burst-16, peak == 16),
  drain order digest-checked, no overflow/error.
- Gate: `make test-full` 199 passed in 5:45 (193 + 6 new week5c2
  nodes); `make repro` (SEED=42) 199 passed in 5:46; `make lint`
  clean. Counts reconcile exactly (no tolerance drift — every pin is
  asserted in-test, so green IS the diff).
- Flake (open risk, honest record): the first `test-full` attempt showed
  ONE failure — `test_p2r_clear_atomic-verilator` (week5b record path,
  untouched this turn) failed a first-attempt assertion, then passed on
  the waves-rerun AND on 7 isolated reruns (5 verilator + 2 icarus),
  unreproduced since. First-attempt log not captured; failure VCD
  preserved at
  `results/week5b_power_record_chain_verilator_test_p2r_clear_atomic_fail.fst`
  (gitignored, local only). No shared state with week5c2 edits
  (separate RTL/TB/build dirs; failure is a single unreproduced event
  in ~9 consecutive greens). If it recurs, the VCD + this note bootstrap
  the investigation; a likely next step is capturing the first-attempt
  assertion text (rerun with `-l`).

## Week 5c, part 3: hmac_core + items 2/5/6/8 (done — commit, then stop)

- Files (this commit): `rtl/hmac_core.v` (replaces the week-1 stub;
  sequencer + key schedule, NO engine — drives one external
  `sha256_wrap`), `tb/hmac_wrap_pair.v` (test-only pair wiring + inject
  mux + taps), `tb/w5_hmac_cocotb.py` (6 cases), `tb/test_week5c_hash.py`
  (6 HMAC CASES added), `edge/receiver.py` (`bad-overrun-range` > 5),
  `tests/test_week5b_overrun_glue.py` (sweep rewritten 0-5/total-255),
  DECISIONS items 2/5/6/8 + hmac measured section (this commit's docs).
- HMAC vectors (all exact, both sims): RFC 4231 cases 1–7 (RTL ==
  stdlib oracle == RFC-published hex fetched from rfc-editor.org; case
  5 asserts the 128-bit truncation prefix; lengths tripwired
  8/28/50/50/20/54/152) + 200 seeded pairs (provision keys, msg 0..200 B
  incl. empty) + wrong-key/msg avalanche + keylen-161 reject + inject
  overflow-abort + full chain→HMAC→chain overlap (item 2: N/N+1 exact,
  window 473 cyc pinned, peak 86 pinned, no overflow/error).
- Measured HMAC jobs (start-stream→done, both sims identical, pinned):
  c1–c5 403 cyc (4 blocks), c6 673 (7: 3 pre + 2 + 2), c7 807 (9:
  3 + 4 + 2). Derived: blocks × 67 + stream + per-pass control.
- Item 6: overrun sweep 0–5 accept (bit 6 iff > 0), 6–255 reject as
  `bad-overrun-range` (receiver, pre-crypto); builder keeps the byte
  clamp (packing, not policy).
- Item 8 mutants (+ wrap mutants from part 2; each reverted;
  `grep MUTANT` clean; survivors: none unexplained):
  | # | mutant | fail scope (week5c file, icarus) | first catcher |
  |---|---|---|---|
  | M-H1 | length bits→bytes | 17/17 | test_hash_nist v1 (v0 empty survives) |
  | M-H2 | length BE→LE | 17/17 | test_hash_nist v1 (v0 survives) |
  | M-H3 | first-block init skipped | 17/17 | test_hash_nist v0 |
  | M-H4 | overflow flag removed | 3/17 (overflow, sealed_force, hmac_overflow_abort) | test_hash_overflow |
  | M-H5 | core mode→SHA-224 | 17/17 | test_hash_nist v0 |
  | M-H6 | abort-reset removed | 12/17 (multi-message only; 5 single-message survive) | test_hash_nist v1 (stale digest) |
  | M-H7 | ipad/opad swapped | 6/6 hmac, 0/11 wrap | test_hmac_rfc17 c1 |
  | M-H8 | long-key pre-hash skipped | 1/17 (rfc17 c6; c1–c5 pass) | test_hmac_rfc17 c6 |
- TB-race lessons (all DUT-exonerated, fixed test-side): (R1)
  `seen`-skip hides pre-poll pulses → `since` param; (R2) one-shot
  asserts outrun same-timestep monitor samples → slack-poll; (R3)
  lone-last before `msg_ready` starves (empty-msg hang) → never signal
  before ready; (R4) stale sticky flags pre-init → verdict window starts
  after re-init.
- Item 7 (CI run for 561f96a): run #22 (id 37841792290), conclusion
  SUCCESS, 20:45–20:48 UTC (≈2m48s), run_attempt 1, all 10 jobs green
  (lint + 9 test files; Verilator 5.048 build SKIPPED = cache hit).
  Node counts per file: 1/28/1/1/8/24/84/1/1 (149 fast-subset nodes).
  Retries: ZERO (no FLAKY-INFRA / "retries initiated" annotations on
  any of the 10 check-runs; logs need admin rights so this comes from
  the annotations API, which the retry parser feeds). Versions pinned
  static from ci.yml: ubuntu-latest, apt iverilog (yml expects ≥ 12;
  exact point version lives only in the admin-walled log),
  Verilator 5.048 tarball (exact), Python 3.12 via uv.
- CI follow-ups (open, not this turn): (a) root-parser false positives
  — the `tests` node shows 2 `failure` annotations quoting assertion
  text from PASSED lines (lookahead excludes FAILED only); tighten to
  real failure contexts. (b) ubuntu-latest migrates to Ubuntu 26 on
  2026-10-19 (deprecation notice in every job) — pin `ubuntu-24.04`
  before the forced move. (c) actions/* Node-20 deprecation warnings —
  bump action versions with (b). (d) run #23 (id 37851823534, tip
  4bcb569, includes 8a36c30 — no isolated run exists for 8a36c30 since
  both pushed together) was IN PROGRESS at report time; confirm green
  next turn. 8a36c30's coverage stands on local gates meanwhile
  (22/22 wrap nodes both sims + lint, above).

## Week 5c, part 2: sha256_wrap + items 1/3/4 (done — commit, then stop)

- Files (this commit): `rtl/sha256_wrap.v` (new; streaming engine
  owning one secworks `sha256_core`), `tb/w5_hash_cocotb.py` (11
  cases), `tb/test_week5c_hash.py` (new; wrap CASES), `git rm
  rtl/hash_chain.v` (week-1 stub, role absorbed by the wrap),
  `tests/test_third_party_manifest.py` (23-file digest pin, new),
  `pyproject.toml` (ruff excludes `third_party/`, E501 ignore for the
  digest table), `.github/workflows/ci.yml` (+ `test_week5c_hash.py`
  matrix line, retry-failed warnings), DECISIONS items 1/3/4 + wrap
  measured section (this commit's docs).
- Wrap vectors (all exact, both sims): NIST (empty/abc/448/896-bit +
  55/56/63/64/119/120) + 1M-bit + 80,068-B window + tomb-slot record
  (item 1: == Python byte string, length unchanged) + 8191/8192/8193
  + back-to-back incl. mid-stream abort + line/mid/stress backpressure
  + ignoring-producer overflow + sealed-force (flag sets, digest
  intact) + fixed KAT `9c239a05…cad1` (item 3) + tail overlap (item 2
  wrap level: withhold, exact, peak 1, 48 core digests).
- Measured (both sims identical, pinned): accept→digest_valid 66
  (== secworks README), re-arm 1 (spacing 67), drain 1, stress peak 221
  (net +3 B/block), line peak 1.
- Item 4: 23-file SHA-256 manifest + full-40-char pin assertion on
  `docs/THIRD_PARTY.md`; any add/modify/delete fails the test.
- Durations (this file, both sims): icarus ~112 s / verilator ~48 s
  for 17 nodes; slowest single nodes 21.9 s (backpressure) / 15.0 s
  (1M) / 9.5 s (window) / 5.7 s (random200) — no slow marks (well
  below the marked-slow week4b-power 123 s; CI runs files in parallel,
  step budget 30 min).
- Gate: `make test-full` equivalent (`pytest -q --durations=15`) 193
  passed in 6:05; `make repro` (SEED=42) 193 passed in 6:08; `make lint`
  clean. (Docs for both parts land in the part-3 commit; this commit is
  RTL + tests + CI-matrix + lint config.)

## Week 5c, part 1: C0 clone + hash reorder (done — STOP before wrapper RTL)

- C0: secworks sha256 cloned to `rtl/third_party/secworks-sha256/`,
  pin `837c5cc396f001d18f2c765721c585716eb439ae` (2025-12-15), BSD
  2-Clause recorded in `docs/THIRD_PARTY.md` (new); nested `.git`
  removed (self-contained tree); core NEVER modified (ruff excludes
  `rtl/third_party/`). Instantiation target `sha256_core` (66
  cycles/block per upstream README; TB-measured figure at build).
- Item 1 (review amendment): `WINDOW_HASH` reordered to
  `SHA256(prev_hash || samples || descriptor)` in
  `edge/attestation.py` (descriptor holds P_AVG/ENERGY, known only after
  the last sample — descriptor-first cannot stream; two-stage rejected
  in DECISIONS.md, SIG/HMAC layouts unchanged). Week 3 tests green with
  ZERO fixture changes (19 passed: no hardcoded digests anywhere —
  build and verify call the same function); reorder proved by direct
  `hashlib` comparison (new order matches, differs from old).
- Amd 4: `record_agg` tomb count simplified u8-saturating → 3-bit
  (0..5 by construction); `sat_inc8` removed as unreachable; 255-sentinel
  gap rule removed from DECISIONS.md (count ≤ 5 exact, bit 6 iff
  count > 0). Re-verified: `tb/test_week5b_power.py` 24 passed both
  sims; mutation table unchanged (no mutant covered removed logic).
- Amd 2/7: DECISIONS.md 5c section (ready/backpressure, FIFO 256,
  sticky overflow, 80 kB/s hash input vs 11.6 MB/s core); one-core
  time-multiplexed schedule + latency formulas (chain 1,252 blocks /
  82,632 cyc + HMAC 5 blocks / 330 cyc ≈ 6.91 ms @ 12 MHz — measured at
  build); threat_model.md HMAC-key-register out-of-scope line.
- Amd 5: CI Report now counts every retry (`retries initiated` notice)
  + warns each retry-failed line (parser proven on synthetic logs).
- Amd 3/6 recorded in DECISIONS.md as the build-phase contract
  (NIST+pad-boundary+80,068 B+8,191/8,192/8,193+back-to-back vectors,
  5 named mutants, provision keys except RFC).
- Gate: `make test` 156 passed, 1 deselected (2:12); `make lint` clean.
- NEXT (needs confirmation): build `sha256_wrap.v` + `hmac_core.v`
  per the amended plan, then the vector/mutant suites.

## Week 5b2: review fixes (done — commit, then stop for 5c planning)

- Week5b2 items (this commit): (a) numbered tables below; (b) 401
  schedule + counting convention rewritten in DECISIONS.md (incl. the
  E398/E399 correction history); (c) `test_p2r_clear_atomic` (clear on
  the close edge, coincident w6 start, next-cycle w7 drop, second record
  carriage) + tombstone gap worked example in DECISIONS.md; (d)
  slow-marker audit (durations table below; only week4b-power is slow);
  (e) override audit: `FERR_CNT_INIT` / `OVERRUN_CNT_INIT` appear only in
  `tb/test_week5b_power.py` `parameters=` (test-only), never in `top.v`
  (still a stub — verified no instantiations) or any synth flow (none
  exists yet); `LUT_MEAN_ENABLE=0` only in `test_week4b_lutparam.py`
  (pre-existing approved pattern); (f) remote/CI status below.
- (a) derivation table, derived vs TB-measured (Verilator 5.048 +
  Icarus 13.0; error = |RTL − golden| in LSB):

  | # | op | derived | measured |
  |---|---|---|---|
  | D1 | DIV unit | done +65 edges (66 incl.) | +65 on all 215 vectors × 2 sims (15 directed incl. mag 2⁶⁴−1 + exact halves ±; 200 random magnitudes/dens) |
  | D2 | SQRT unit | done +33 edges (34 incl.) | +33 on all 223 vectors × 2 sims (23 directed incl. rem==root/no-round, rem==root+1/round, 2⁶⁴−1 → 2³²; 200 random + squares) |
  | D3 | window | 401 incl. (E0→E400) | 401 on 1000 vectors × 2 sims (max/mean LSB error 0 on every field) + 13 directed × 2 + 8 real-rate windows × 2 (all fields exact, p_avg ≠ 0) |
  | D4 | tombstone | out 1 edge after drop win_end | +1 on all drop tests (incl. deferred past completions) |

- (a) mutation table, fail scope + first catcher (each reverted;
  `grep MUTANT` clean; survivors: none):

  | # | mutant | fail scope | first catcher |
  |---|---|---|---|
  | M1 | M-DIVSUB (restoring `>=`→`>`) | 4/8 divsqrt nodes | test_div_directed / verilator |
  | M2 | M-DIVREM (half-away `>=`→`>`) | 2/8 (directed only; randoms pass) | test_div_directed / verilator |
  | M3 | M-SQRTCMP (`rem>root`→`>=`) | 2/8 (directed only) | test_sqrt_directed / verilator |
  | M4 | M-SIGN (negation dropped) | 4/8 | test_div_directed / verilator |
  | M5 | M-SNAP (E leaks into shadow) | 19/23 power+week4b nodes | test_chain_spurious_overrun / verilator |
  | M6 | M-DROPNOFLAG (drop sans flag) | 10/22 power nodes (clean nodes pass) | test_chain_spurious_overrun / verilator |
  | M7 | M-CNTWRAP (overrun guard removed) | 2/22, saturate nodes only | test_fsm_overrun_saturate / verilator (reads 0001) |
  | M8 | M-BIT6OFF (bit 6 never set) | 3 nodes (record + p2r ×2) | test_week4b_record node |
  | M9 | M-FERRGUARD (framing guard removed) | 2/22, ferr_init nodes only | test_ferr_init_saturate / verilator |
  | M10 | M-WDROP0 (`w_dropped` stuck 0) | 4 nodes (p2r ×2 sims ×2 cases) | test_p2r_clear_atomic / verilator |

- (c) atomic-clear finding (honest): the first snapshot+clear design had
  NO race-free timing (record_agg samples one edge after power's edge, so
  any close-covering clear wipes the next snapshot while deferring it
  strands tombstone evidence) — the test caught it, and the design moved
  to tomb-flag counting (evidence atomic with its slot; power counter
  pure telemetry). `test_p2r_clear_atomic` now pins: on-close clear
  leaves rec1 (count 1) intact, coincident w6 starts exact, next-cycle
  w7 drops fresh-counted, record 2 carries (slot order [tomb, w6, ...]).
- (d) slow-marker audit — full-suite `--durations` (this turn, 149
  passed): only `test_week4b_power` exceeds 60 s (125.1 s, 1000 vectors
  × 2 sims) and carries the sole `slow` mark. Next slowest:
  frame-saturate 42.3 s / 19.1 s (icarus/verilator), week4b_uart 23.5 s,
  p2r_clear_atomic 8.6 s (new), lutparam 7.0 s, fsm_real_rate 11.5 /
  4.7 s — all fast by the rule. `make test` (148) ≈ 2 min;
  `make test-full` (149) ≈ 4-6 min.
- (f) remote/CI: repo URL received at sign-off
  (`RADICAL-devp/Hardware-Root-of-Trust-HRoT-for-Industrial-MRV`);
  pushing post-commit, then polling the Actions run. Versions noted:
  local Icarus 13.0 / Verilator 5.048 proven; CI installs apt iverilog
  (noble: 12.0, verified on packages.ubuntu.com) + Verilator 5.048
  tarball (cached); first green CI run is the compatibility
  confirmation for Icarus 12.
- (f2) CI runs (post-commit polling): run #1 RED in 6m03s at step "Use
  pinned Verilator" — my workflow bug (`$GITHUB_PATH` applies to later
  steps only), fixed in `week5b2: fix CI PATH` and pushed. Run #2 RED in
  ~13 min at "Test (fast subset)"; per-test logs need auth (403), but
  the checks API gave step conclusions + the repo now emits `::error::`
  per failed node and `::warning::` per FLAKY-INFRA pass (publicly
  readable). Icarus version compatibility (12.0 apt vs 13.0 local)
  could not be confirmed from docs (cocotb publishes no hard floor);
  a local Icarus-v12 source build was attempted and abandoned
  (macOS toolchain drift: bison grammar, then SDK headers) in favor of
  evidence below.
- (f3) nondeterministic Verilator crashes (measured, both sims' runs):
  1 SIGSEGV first-attempt (`terminated with error -11`) in a local fast
  run + 1 more failed chain-verilator node in 20 targeted reruns, each
  passing on immediate rerun — ~0.1%/node background rate, consistent
  with the week5a 1-off and plausibly the CI red. Policy (in
  `tb/sim_retry.py`, unit-tested, all 8 wrappers): a dead simulator
  renders NO verdict, so exactly one retry is allowed; a clean rerun
  passes WITH a `FLAKY-INFRA-PASS` banner (CI warning annotation +
  counted here), while ANY assertion failure — first attempt or rerun —
  still fails hard. Rates are tracked, not hidden; a climbing rate gets
  a root-cause task (Verilator/VPI internals). No verdict is ever
  converted, only no-verdict crashes recovered.
- (f4) CI GREEN twice in a row (runs #17 `59b0834`, #18 `6e6cafa`:
  lint + all 9 matrix jobs green, Icarus 12.0 included — the
  compatibility confirmation). The green transition correlates exactly
  with `rm -rf /tmp/verilator-5.048` (~2-3GB source+objects reclaimed):
  disk pressure is the likely root cause of the 34 deterministic
  verilator-skewed failures (Verilator is disk-hungry for compiles;
  Icarus/python are not). Stated honestly: correlation is exact
  (red-before/green-after ×2) but the mechanism is inferred, not
  log-proven; falsifier is any future red with different victims (then
  reopen). Resource-snapshot step re-added to keep disk/mem visible.
  Remaining CI debt: cache never saved before first green (cold every
  time until then); per-job Verilator tarball builds run in parallel
  (~5 min wall, wasteful but bounded).
- Verified for this commit (`make test-full`: 149 passed incl. the new
  atomic node; `make repro` twice: 149 passed each, shasums identical —
  `metrics.json` `b1ea2876…`, `week4b.json` `e50838df…`, sim JSONs with
  401s); ruff clean.

## Week 5b: divider/sqrt FSMs + finalization/overrun (done, awaiting review)

- Done: `rtl/div_fsm.v` (restoring 64-bit, half-away, done +65 edges),
  `rtl/sqrt_fsm.v` (restoring 32-iter, rem>root round-up, 33-bit root,
  done +33), `rtl/power_calc.v` reworked on a linear 401-cycle schedule
  (edge map in DECISIONS.md), tombstone drops with saturating
  `finalize_overrun[_cnt]`, `rtl/record_agg.v` overrun carry (tomb-flag
  counting as of week5b2 — the week5b snapshot scheme proved untestable
  for atomic clear and was replaced; see week5b2 section), descriptor
  `reserved` byte → overrun count +
  bit-6 rule in `edge/`, `uart_rx.framing_err_cnt` + `FERR_CNT_INIT` /
  power `OVERRUN_CNT_INIT` test-only params, `cnt_clear` fanout scheme.
- Derivation vs measured (TB-measured, both sims, both rates):

  | op | derived | measured |
  |---|---|---|
  | DIV (each of 5) | done +65 edges (66 incl.) | 65 exactly, all vectors |
  | SQRT (each of 2) | done +33 edges (34 incl.) | 33 exactly, all vectors |
  | window (win_end E0 → out_valid E400) | 401 inclusive | 401 on 1000 vectors + directed + 8 real-rate windows |

- Verified (`make test-full`: 147 passed; `make repro` twice: 147 passed
  each): 1000 seeded vectors + every week4b directed case byte-exact
  (both sims); real-rate node (M=48 TB-driven, nonzero codes, 8 windows
  × 1200 clocks/sample, all fields exact + 401); overrun/tombstone/
  recovery, shared-E both rates, full-scale-E drop (E attributed once),
  timing edges (before=drop / on=start / after=normal), min-spacing,
  zc-driven spurious storm (modeled drop count exact, 2 recovery windows
  exact), power→record chain (arrival-order slots, count 1, bit 6),
  88-flip/CRC/hole/reset/clear suites re-green; `results/week4b_*.json`
  regenerated with latency 401/401 (expected change).
- Mutations, first catchers (each reverted; `grep MUTANT` clean;
  survivors: none): M-DIVSUB → div_directed/verilator; M-DIVREM →
  div_directed/verilator (randoms pass — halves need directing);
  M-SQRTCMP → sqrt_directed/verilator (rem==root case); M-SIGN →
  div_directed/verilator; M-SNAP → chain_spurious_overrun/verilator
  (all power nodes fail); M-DROPNOFLAG → chain_spurious_overrun/
  verilator (all 10 overrun nodes); M-CNTWRAP → overrun_saturate
  exclusively (INIT FFFE + 3 drops reads 0001); M-BIT6OFF → week4b
  record node (then p2r); M-FERRGUARD → ferr_init_saturate exclusively.
- Honest notes: two TB-side (not RTL) bugs found by testing — (1) PF
  holding latch first placed on the divider's own latch edge (stale
  readback); (2) `await_out` missing coincident pulses / re-catching
  consumed ones (check-first + fresh protocol). Tombstones fill record
  slots in arrival order (documented; golden glue uses arrival order).
  `results/metrics.json` + `results/week4b.json` byte-identical to
  week4b2 (`b1ea2876…`, `e50838df…`); sim JSONs carry the new 401s.
- Infra (amendments 7/8 + follow-ups): `make test` = fast subset (146,
  `slow` = week4b 1000-vector suite only, >60 s rule), `make test-full`
  = everything, `make repro` = full; AGENTS.md gains the gate lines;
  CI pins Verilator 5.048 tarball (cached) + apt iverilog (noble: 12.0;
  local 13.0 proven — first green CI run is the confirmation) + fast
  subset; `make ci-local` for no-remote bootstrap. CI itself unrun
  (no remote configured).
- Open risks: uart ferr guard proven by INIT-override (natural proof
  infeasible at line rate — stated openly); power overrun guard twin
  (INIT-override + 3-drop saturation test); simulation proves
  logic/math/detection only.
- Next (Week 5c): secworks SHA-256 clone + THIRD_PARTY.md + HMAC core.
  STOP — awaiting review before 5c.

## Week 5a2: review fixes (done, awaiting re-review)

- Done (all 6 review items):
  1. Flake campaign: per-case per-sim `sim_build` dirs
     (`week5a_{top}_{sim}_{case}`) + `WEEK5A_SIM` filter + one pytest
     node per (case x sim). 50x week5a matrix per simulator: 50/50
     iterations x 12 nodes green on Verilator (600 node-passes) and 50/50
     on Icarus (600 node-passes), zero failure signatures in
     `sim_build/week5a2_flake/` logs. Full suite 5x: 103 passed each, zero
     failures. The single week5a Verilator failure from the week5a turn is
     unreproduced across 1200 node-passes + 5 suites; no root cause ever
     established (the wrapper now prints the root cocotb error before the
     waves rerun, so any recurrence is diagnosable). A 6th full suite
     (105 nodes incl. the new saturation case, below) also passed.
  2. Resync proof: `test_frame_resync_two_a5` (bad-CRC candidate with two
     0xA5, valid frame at the second; geometries (4, 8) and (1, 5), 2x
     `crc_err`, counters (2, 2), byte-exact vs golden both sims) +
     `test_frame_fuzz_corrupt` (64 seeded streams x ~320 B mixing valid
     frames/garbage/1-3-bit flips/truncations/SOF runs; frames AND
     crc/resync counts exact vs `parse_l0_stream` + SOF-scan reference
     derived from `parse_l0_stream` windows — frames and counts share the
     ONE golden CRC, confirmed — zero diffs both sims). Replay/overrun stated in
     DECISIONS.md: 0-cycle stall, overrun structurally impossible; TB
     drives 1 B / 2 clocks (~40-80x line rate) as stress.
  3. Added: `test_frame_flips88` (all 88 single-bit flips through RTL;
     SOF-byte flips destroy sync so counts come from the reference, every
     flip loses the base frame, RTL == golden), `test_frame_a5_in_crc`
     (CRC bytes == 0xA5 parse positionally), `test_hole_...` (test-only
     `tb/uart_frame_int.v` wiring `uart_rx` -> `frame_rx`: one bad stop
     bit deletes exactly one byte, straddling frame lost, neighbors
     survive, golden computed over emitted bytes), `test_frame_reset_...`
     (silence under reset incl. driven bytes, exact parse after, counters
     post-reset-only). One pytest node per (case x sim): 13 cases x 2
     sims = 26 nodes, each reporting separately.
  4. Mutations (each reverted; `grep MUTANT` clean; survivors: none):
     M-a CRC init FFFF->0000 CAUGHT 24/24 nodes; M-b poly 1021->8408
     CAUGHT 24/24; M-c frame length 11->10 CAUGHT 24/24; M-d CRC gate
     removed CAUGHT by all 16 rejection-containing nodes (first: bad_crc;
     the 8 all-good nodes pass, expected — the gate is invisible on
     CRC-valid streams); M-e rescan branch-1 off-by-one CAUGHT by
     resync_two_a5 x2 (the (1, 5) geometry pins branch 1) + fuzz x2;
     M-f crc saturation guard removed CAUGHT exclusively by the
     counters_saturate nodes x2 — wrapped readback (9991, 65535) vs
     expected (65535, 65535), all other 24 nodes pass (small counts never
     reach the guard).
  5. Counters: 16-bit saturating `crc_err_cnt`/`resync_cnt` in RTL
     (sticky at 0xFFFF, cleared on reset; resync counts iff
     `rescan_pos != 0`) plus a sticky `cnt_saturated` flag set when
     either counter reaches 0xFFFF (top.v treats saturated windows as
     lower bounds — DECISIONS.md Week 5b forward rules), pinned by
     `test_frame_counters_saturate` (flag 0 on small counts, 1 after
     saturation, 0 after reset; hole test proves the `uart_frame_int`
     passthrough):
     increment-exact vs reference from reset, then natural saturation —
     simulator VPI writes (deposit AND force) demonstrably do not land
     on these Verilator flops (measured readback 0x0000), so 70,000
     bad-CRC frames with inner A5 drive 141,063 rejects AND resyncs
     (~2.15x margin), both counters read 0xFFFF on both sims.
     Monotonicity: enforced ONLY in Python (`edge/receiver.py`,
     `ledger/verifier.py`); `frame_rx` outputs the counter verbatim
     (DECISIONS.md Week 5a2).
  6. `make repro` twice (105 passed each): `results/metrics.json`
     `b1ea2876…`, `results/week4b.json` `e50838df…`,
     `results/week4b_verilator.json` `8e78b4d1…`,
     `results/week4b_icarus.json` `9a246faa…` — byte-identical
     before/after both runs AND matching the week4b2-recorded shasums
     (week5a2 touches no metrics).
- Verified: week5a file 26 passed (both sims); full `pytest` 105 passed;
  `ruff check` + `ruff format --check` clean.
- Open risks: original flake unexplained (see above; 1200+ clean
  node-passes since); `CLK_PER_BIT` still 16 (12 MHz decision deferred);
  divider/sqrt still behavioral loops (Week 5b FSMs); simulation proves
  logic/math/detection only — nothing about physical tamper resistance.
- Forward note for 5d (VPI lesson, directive): simulator VPI writes
  (cocotb deposit AND force) do not land on Verilator flops — measured
  readback stays 0x0000 — so 5d tests that need near-limit state
  (monotonic counter near 2^32, HMAC nonce-range exhaustion) MUST use a
  test-only parameter for the initial value (e.g. INIT/RESET_VALUE),
  never VPI writes. `frame_rx`'s own 16-bit saturation was proven
  naturally (70k frames); a 32-bit counter cannot be, hence the
  parameter. `tb/uart_frame_int.v` is referenced only by tb/ + docs —
  confirmed in no Yosys/synth/Makefile/CI file list (none exists yet;
  the Yosys step must keep it that way).
- CI status (reported, not fixed here): `git remote -v` is EMPTY (no
  remote configured) and `gh` is not installed (`brew install gh` +
  `gh auth login` to query), so no GitHub CI result could be fetched.
  Finding for review: `.github/workflows/ci.yml` installs Verilator only
  — every dual-sim node also needs Icarus, so CI as configured fails all
  `*-icarus` legs; fix is one line (`sudo apt-get install -y iverilog`)
  but left for your approval. Local runtimes (this turn, SEED=42):
  week5a matrix both sims ~65-160 s (saturate node dominates: ~18 s
  Verilator / ~38 s Icarus); full suite ~355-417 s; 50x campaign iters
  ~5 s/sim; `make repro` ~409-415 s.
- Next (Week 5b): iterative divider/sqrt FSMs with documented cycle
  count, exact match to golden. STOP — awaiting review before 5b.

## Week 5a: frame_rx L0 framer (done, awaiting review)

- Done: `rtl/frame_rx.v` (SOF hunt, 11-byte assembly, CRC16-CCITT-FALSE
  gate, rescan resync — explicit case statements, no variable-index
  writes; Verilog-2005). Contract in DECISIONS.md Week 5a: byte pipe
  only, continuity stays in Python. Tests first: `tb/w5_frame_cocotb.py`
  (6 cocotb cases vs `parse_l0_stream`: good/back-to-back, garbage
  prefix + truncated-tail buffering + tail completion, bad CRC with
  `crc_err`, cut-mid-frame resync, SOF-in-payload, 300-frame seeded e2e)
  via `tb/test_week5a_frame.py` (Verilator 5.048 + Icarus 13.0 wrapper
  that now prints the root error before the waves rerun).
- Verified: `tb/test_week5a_frame.py` 1 passed (both sims);
  full `pytest` 80 passed; `ruff check` + `ruff format --check` clean.
- Honest flake note: the week5a test failed ONCE in a full-suite run
  (Verilator leg; root log swallowed by the old wrapper — the reason for
  the print improvement) and has passed 4/4 runs since, including two
  full suites (80 passed). Vectors are deterministic; no root cause
  established. If it recurs, the wrapper now surfaces the cocotb error
  and saves the FST to `results/week5a_frame_rx_*_fail.fst`.
- Open risks: `CLK_PER_BIT` still 16 (12 MHz / 1.5 Mbaud / 8 clocks
  decision + baud re-measurement deferred to a later Week 5 step);
  divider/sqrt still behavioral loops (Week 5b FSMs); simulation proves
  logic/math/detection only — nothing about physical tamper resistance.
- Next (Week 5b): iterative divider/sqrt FSMs with documented cycle
  count, exact match to golden. STOP — awaiting review before 5b.

## Week 4b2: review fixes (done, awaiting re-review)

- Done (all 8 review items): (1) divider/sqrt declared combinational in
  `rtl/power_calc.v` + DECISIONS.md Week 4b2 — "3 cycles" is behavioral-sim
  depth only, Week 5 builds divider/sqrt FSMs and re-states the count;
  `results/week4b.json` + the Week 4b note above reworded accordingly.
  (2) Descriptor P_AVG source DECIDED: `p_avg_exact` only (DECISIONS.md,
  comments in `edge/attestation.py` + `tb/golden.py`; both were already
  exact-only — verified zero LUT references in `edge/`); `p_avg_lut`
  behind `LUT_MEAN_ENABLE` (0 ties it to 0), proven by the new
  exact-only-build test both sims. (3) Record-level NEG_ENERGY clamp now in
  RTL (`rtl/record_agg.v`: signed increments over valid slots + prev added
  once + clamp once → 0 + flags bit 5; carries `rec_p_sum`/`rec_m_total`/
  5×M/flags for the descriptor); mixed-sign (+,+,-,-,+) records proven for
  negative AND positive totals plus exact-zero (no flag) vs
  `record_fields`/`build_record` byte-for-byte, back-to-back + reset.
  (4) Clock/baud stated (TB 10 ns, 16× → 6.25 Mbps sim; L0 needs ≥ 1.1 Mbps
  = 11 B × 10 b × 10 kHz, so 115200 baud cannot carry it); baud edge
  MEASURED both sims (pass −3.5 %/+5 %, fail −4 %/+5.5 %, ±2 % pinned with
  ≥1.5 pp margin); `frame_rx.v` added to Week 5 scope (DECISIONS.md).
  (5) 88-bit truncation mutant SURVIVES — honestly reported: no in-contract
  test can observe it (max product 2.22e16 < 2^64; invalid-window garbage
  forced to 0); 88-bit stays as defense-in-depth with the bound in a
  comment. (6) Trailing `win_start_pulse` confirmed as original Week 4a2
  spec ("E owned by the next window") in DECISIONS.md; TB expects golden
  starts + last golden end. (7) Forced failure (+6 % baud) demonstrated:
  `results/week4b_uart_rx_{verilator,icarus}_fail.fst` landed via the
  harness (removed after; gitignored). Waveforms: Verilator FST
  (`dump.fst`, needs trace-capable build + `--trace` — harness now wires
  `-CFLAGS`/`OPT_FAST` defines; root-caused a shared-stale-`verilator.o`
  clobbering + a link rule requiring model+harness trace consistency, fixed
  by making every Verilator build trace-capable), Icarus FST
  (`<toplevel>.fst` under waves); neither emits VCD in this flow.
  (8) Python pinned 3.12: `pyproject` `==3.12.*` (lock re-resolved,
  0 packages changed), AGENTS.md + CI already 3.12, local `.venv` rebuilt
  on 3.12.13; week4b JSONs byte-identical under 3.14 and 3.12.
- Mutations (each reverted; `grep MUTANT` clean): M1 div `>=`→`>` CAUGHT
  by `test_power_directed` (halves); M2 win-start no-reset CAUGHT by
  `test_record_5x` ([S,E) boundary); M3 LUT[0] 9269→9270 CAUGHT by
  `test_lut_sweep_421`; M4 ARM −328→−300 CAUGHT by new
  `test_zc_arm_threshold` (±1-code pin; would have SURVIVED the old suite —
  the test is the fix); M5 NEG bit 5→4 CAUGHT by mixed-sign record tests;
  M6 88-bit→64-bit product SURVIVOR (bound proof above, no test possible
  in-contract — reported, not hidden).
- Verified: `make repro` twice on Python 3.12.13 — 79 passed both times
  (77 + record_agg + lutparam suites), `ruff check` + `ruff format --check`
  clean; `results/week4b_{verilator,icarus}.json` shasum-identical across
  both runs AND the 3.14 run (`8e78b4d1…`, `9a246faa…`); `metrics.json`
  untouched (`b1ea2876…`).
- Open risks: Week 5 divider/sqrt FSM area unknown (Yosys decides);
  `rec_p_avg` exact division is Week 5 descriptor scope; simulation proves
  logic/math/detection only — nothing about physical tamper resistance.

## Week 4b: RTL + cocotb (done)

- Done: `rtl/uart_rx.v` (8N1 byte RX, 16x mid-bit sample, bad-stop drop +
  `framing_error`), `rtl/zc_detect.v` (ARM <-328 codes, trigger first >= 0
  while armed, counter-index boundaries, gap [150, 260] + M [1810, 2230]
  range comparators), `rtl/power_calc.v` (64-bit accumulators, dual P_AVG:
  `p_avg_exact` == `mean_q30_half_away` is the byte-exact descriptor target,
  `p_avg_lut` == `lut_window_mean` via generated 512x24 ROM
  `rtl/recip_lut.vh` from `sim/gen_recip_lut.py`; restoring divider/sqrt,
  exact PF, integer-exact signed energy) — all Verilog-2005, no latches,
  no delays. `tb/golden.py::record_fields` gained the NEG_ENERGY clamp
  (total < 0 → 0 + flags bit 5, P_AVG signed-exact) mirroring
  `edge/attestation.py`, pinned by `tests/test_week4b_golden.py`.
- Verified (SEED=42): `pytest` 77 passed (70 prior + 4 golden-clamp + 3
  dual-sim wrappers), `ruff check` + `ruff format --check` clean.
  zc→power interface/timing exactly per DECISIONS.md (strobes live during
  the detection-sample cycle; [S, E) with E owned by next window; invalid →
  recorded M 0 + all-zero outputs). Directed: every window boundary,
  missing crossing, noise glitch, sag, freq step, M 1809/1810/2230/2231,
  full-scale pos/neg, full-negative-DC clamp, negative-P record (energy 0
  + bit 5 + exact P_AVG, equals `record_fields` and `build_record`
  byte-for-byte), accumulator headroom (2230 full-scale both signs),
  rounding halves (+/-), UART bad-stop/back-to-back/cut-mid-byte/±2 %
  baud/reset-mid-window, 300-frame L0 e2e byte-exact. All 421 M values
  through the LUT, exact. 1,000 seeded vectors under BOTH Verilator 5.048
  and Icarus 13.0: max/mean LSB error 0 on every exact field
  (`results/week4b_verilator.json`, `results/week4b_icarus.json`,
  merged in `results/week4b.json`).
- Measured (actual runs, not assumed): BEHAVIORAL-simulation pipeline
  depth exactly 3 per window (last-sample → `out_valid`) on all 1,000
  vectors × 2 sims — not a hardware timing claim (see Week 4b2:
  divider/sqrt are combinational loops; Week 5 FSMs re-state the count).
  LUT-vs-exact worst relative error 6.61e-05 (0.0066 %) — reproduces the
  D-05 adopted sizing claim. Zero failing vectors, so no failure VCDs exist
  (harness reruns any failure with waves and copies the dump to `results/`).
- Open risks: dividers/sqrt are behavioral loops (sim-exact; Week 5 Yosys
  decides area, fallback alt 3 documented in D-05); simulation proves
  logic/math/detection only — nothing about physical tamper resistance;
  env runs Python 3.14 while AGENTS.md says 3.12 (not chased).

## Week 1: Foundations (done)

- Done: repo root collapsed to `hrot/`; `docs/BLUEPRINT.md` present;
  `docs/DECISIONS.md` + `docs/threat_model.md` written; scaffold per
  blueprint §3 + `sim/`; hello-world cocotb test via Verilator passes.
- Verified: `make test` (1 passed), `make lint` (ruff check + format
  clean), `make repro` (SEED=42) green on 2026-10-06.
- Open risks: icarus-verilog + gtkwave missing (`brew install
  icarus-verilog gtkwave` when needed); Yosys present, unused until Week 5.

## Week 4a3: Width + policy fixes (done)

- Done: record-level width correction (45-bit signed record sums;
  E_rec_max 15,485,165 µWh; full-scale all-5-valid directed test both
  signs); NEG_ENERGY bit-5 flips rejected both directions + per-record
  `neg_energy_clamped` metadata with batch counting (5 of 20);
  threshold-rounding pin test (-328/0 as intended rounding of -5.0/0.0);
  PF exact-match contract for Week 4b.
- Verified: `pytest` 70 passed, `ruff check` + `ruff format --check`
  clean. `make repro` twice identical shasum on `results/metrics.json`.
- Open risks: P_AVG LUT-vs-exact register mapping to be stated in Week 4b.

## Week 4a2: Review fixes (done)

- Done: integer-code detector core (`find_rising_crossings_q15`, arm <
  -328 codes = -5.0049 V, trigger ≥ 0) with volts wrapper proven equal on
  200 seeded signals + exhaustive 4096-code map check; NEG_ENERGY clamp
  (bit 5, `P_AVG` signed-exact, still verifies); old-vs-new rounding
  property test (differ only on exact halves, 1 LSB); measured
  interpolation accuracy (integer 0.1386 % vs fractional 0.1261 % max —
  sensor floor dominates, interpolation stays out of RTL).
- Verified: `pytest` 66 passed, `ruff check` + `ruff format --check`
  clean. Week 3 fixtures/expected values unaffected (no test pinned
  absolute values; 0/24 real windows differed by the rounding change).
  Week 2b `metrics.json` byte-identical after the detector refactor
  (shasum-compared rerun of `run_week2b.py`).
- Open risks: same as Week 4a (sqrt/division RTL unproven until 4b).

## Next (Week 5: Verilog core part 2)

- Hash chain (secworks SHA-256) + HMAC, counter logic, `top.v` wiring the
  Week 4b modules; Yosys LUT/FF/BRAM counts; RTL ledger verifies in
  `verifier.py`.

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
