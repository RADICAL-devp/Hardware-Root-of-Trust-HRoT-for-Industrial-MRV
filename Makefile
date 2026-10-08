.PHONY: test test-full lint repro sync ci-local

sync:
	uv sync

test:
	uv run pytest -m "not slow" -q

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
