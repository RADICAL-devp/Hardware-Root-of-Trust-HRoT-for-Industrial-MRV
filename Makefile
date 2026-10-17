.PHONY: test test-quick test-full lint repro sync ci-local synth mutants-week6k mutants-week6l attacks-week7

sync:
	uv sync

synth:
	uv run python synth/run_synth.py

test:
	uv run pytest -m "not slow" -q

# Fast per-edit tier: Week 6/7 Python suites only (no RTL sims). For rapid
# iteration; does NOT replace make test / test-full / repro as gates.
test-quick:
	uv run pytest tests/test_week6_mpc.py tests/test_week6_baseline.py tests/test_week6_integration.py tests/test_week6c_stageA.py tests/test_week6c_json.py tests/test_week6d_capability.py tests/test_week6f_inaccurate.py tests/test_week6k_dq.py tests/test_week6l_coreloss.py tests/test_week7_attacks.py -q

test-ci:
	uv run pytest -m "not slow" -v --durations=20

test-full:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

repro:
	SEED=42 uv run pytest -v

ci-local:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest -m "not slow" -q

# Week 6K mutation check (NOT part of a gate; rewrites plant/dq_machine.py
# then restores it). Shows the dq test suite catches broken physics.
mutants-week6k:
	uv run python sim/mutants_week6k.py

# Week 6L mutation check (same safety: rewrites then restores plant/dq_machine.py).
mutants-week6l:
	uv run python sim/mutants_week6l.py

# Week 7 attack campaign (NOT part of a gate; rewrites results/metrics.json).
# Runs each in-scope attack N_TRIALS times and records the detection table.
attacks-week7:
	uv run python sim/attacks_week7.py
