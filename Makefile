.PHONY: help install demo test test-integration lint eval infra up down migrate api worker dashboard demo-stack e2e doctor

PY ?= python
COMPOSE ?= docker compose
LOCAL_DB ?= postgresql://postgres:postgres@localhost:5432/prreview
LOCAL_REDIS ?= redis://localhost:6379/0

help:  ## list the targets
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

install:  ## install the backend with dev tools into the current environment
	$(PY) -m pip install -e ".[dev]"

demo:  ## review a sample pull request offline: no accounts, no keys, no services
	$(PY) -m backend.cli demo --case hardcoded-secret --approve

test:  ## unit tests (in-memory, about five seconds)
	$(PY) -m pytest -q

test-integration: infra  ## the same suite plus Postgres/Redis tests (uses its own test database)
	-$(COMPOSE) exec -T db psql -U postgres -c "CREATE DATABASE prreview_test" >/dev/null 2>&1
	TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/prreview_test TEST_REDIS_URL=redis://localhost:6379/5 $(PY) -m pytest -q

lint:  ## ruff
	$(PY) -m ruff check backend tests scripts

eval:  ## golden set + regression gate (rule packs, or your configured model)
	$(PY) -m backend.cli eval

infra:  ## start Postgres (TimescaleDB + pgvector + pgvectorscale) and Redis
	$(COMPOSE) up -d --wait db redis

up:  ## start everything in containers, dashboard on http://localhost:3000
	$(COMPOSE) --profile app up -d --build

down:  ## stop the containers (data volumes are kept)
	$(COMPOSE) --profile app down

migrate:  ## apply database migrations to TIGER_DATABASE_URL
	$(PY) -m backend.cli migrate

api:  ## run the API with reload
	uvicorn backend.main:app --reload --port 8000

worker:  ## run the queue worker
	arq backend.job_queue.worker.WorkerSettings

dashboard:  ## run the dashboard in dev mode
	cd frontend && npm install && npm run dev

demo-stack:  ## a populated system with fake GitHub and model: then `make dashboard`
	$(PY) -m scripts.dev.demo_stack

e2e: infra  ## multi-process drill: webhook -> queue -> worker -> Postgres, incl. kill-and-resume
	-$(COMPOSE) exec -T db psql -U postgres -c "CREATE DATABASE prreview_e2e" >/dev/null 2>&1
	TIGER_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/prreview_e2e REDIS_URL=redis://localhost:6379/1 $(PY) -m scripts.dev.e2e

doctor:  ## show the resolved configuration and check connectivity
	$(PY) -m backend.cli doctor
