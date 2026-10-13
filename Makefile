.PHONY: test test-quick test-full lint repro sync ci-local synth

sync:
	uv sync

synth:
	uv run python synth/run_synth.py

test:
	uv run pytest -m "not slow" -q

# Fast per-edit tier: Week 6 Python suites only (no RTL sims). For rapid
# iteration; does NOT replace make test / test-full / repro as gates.
test-quick:
	uv run pytest tests/test_week6_mpc.py tests/test_week6_baseline.py tests/test_week6_integration.py tests/test_week6c_stageA.py tests/test_week6c_json.py tests/test_week6d_capability.py tests/test_week6f_inaccurate.py -q

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
