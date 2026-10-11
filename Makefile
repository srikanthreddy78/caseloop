.PHONY: install test lint format typecheck dev demo demo-live eval eval-live migrate db-create create-business add-owner

install:
	cd backend && uv sync --extra dev

test:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check app tests scripts
	cd backend && uv run ruff format --check app tests scripts

format:
	cd backend && uv run ruff format app tests scripts
	cd backend && uv run ruff check app tests scripts --fix

typecheck:
	cd backend && uv run mypy app

# Create local databases on a Homebrew/any Postgres reachable with psql.
db-create:
	psql -h 127.0.0.1 -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='caseloop'" | grep -q 1 || psql -h 127.0.0.1 -d postgres -c "CREATE DATABASE caseloop"
	psql -h 127.0.0.1 -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='caseloop_test'" | grep -q 1 || psql -h 127.0.0.1 -d postgres -c "CREATE DATABASE caseloop_test"
	psql -h 127.0.0.1 -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='caseloop_scratch'" | grep -q 1 || psql -h 127.0.0.1 -d postgres -c "CREATE DATABASE caseloop_scratch"

migrate:
	cd backend && uv run alembic upgrade head

dev:
	cd backend && CASELOOP_ENV=dev uv run uvicorn app.main:app --reload --port 8000

create-business:
	cd backend && uv run python -m app.cli create-business $(ARGS)

# Give a person owner access to a business's dashboard: ARGS="--slug acme --email you@acme.example"
add-owner:
	cd backend && uv run python -m app.cli add-owner $(ARGS)

# Scripted agent + fake channels: no network, no keys.
demo:
	cd backend && uv run python scripts/demo.py

# Real OpenAI model drives the tools; channels still fake (nothing is sent).
demo-live:
	cd backend && uv run python scripts/demo.py --live

# Agent evaluation set (backend/evals/cases.yaml): scripted runner is the baseline.
eval:
	cd backend && uv run python scripts/eval.py

# Same cases, real OpenAI model; costs money, expects at least 80% to pass.
eval-live:
	cd backend && uv run python scripts/eval.py --live --min-pass 0.8
