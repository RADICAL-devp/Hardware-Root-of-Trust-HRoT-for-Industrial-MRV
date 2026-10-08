# PROGRESS

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
