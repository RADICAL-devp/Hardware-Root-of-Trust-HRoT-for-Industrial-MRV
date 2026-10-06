.PHONY: test lint repro sync

sync:
	uv sync

test:
	uv run pytest -v

lint:
	uv run ruff check .
	uv run ruff format --check .

repro:
	SEED=42 uv run pytest -v
