# Single entry points so humans and CI run identical commands.
BACKEND_PY := backend/.venv/bin

# Load-test knobs (#10) — override on the CLI, e.g. `make loadtest USERS=200`.
LOADTEST_HOST  ?= http://localhost:8000
USERS          ?= 50
SPAWN_RATE     ?= 10
RUN_TIME       ?= 5m

# Where `make test-pg` points the postgres suite. Override to reuse a database
# you already have: `make test-pg POSTGRES_TEST_DSN=postgresql://...`.
PG_TEST_CONTAINER ?= trialgate-test-pg
POSTGRES_TEST_DSN ?= postgresql://postgres:trialgate@localhost:55432/trialgate_test

.PHONY: lint format typecheck test test-pg check eval loadtest loadtest-ui

lint:
	cd backend && $(CURDIR)/$(BACKEND_PY)/ruff check app tests
	cd frontend && npm run --silent lint

format:
	cd backend && $(CURDIR)/$(BACKEND_PY)/ruff format app tests
	cd frontend && npm run --silent format

typecheck:
	cd backend && $(CURDIR)/$(BACKEND_PY)/mypy
	cd frontend && npm run --silent typecheck

test:
	cd backend && $(CURDIR)/$(BACKEND_PY)/python -m pytest -q

# The postgres store suite, against a throwaway container (#97). Skipped by a
# plain `make test`, which needs no database — CI runs it against a service
# container, and this is the same suite for anyone touching persistence.py.
# Port 55432 so it never collides with a local postgres on 5432.
test-pg:
	@docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(PG_TEST_CONTAINER) -p 55432:5432 \
		-e POSTGRES_PASSWORD=trialgate -e POSTGRES_DB=trialgate_test postgres:16-alpine
	@echo "waiting for postgres..."
	@until docker exec $(PG_TEST_CONTAINER) pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
	-cd backend && POSTGRES_TEST_DSN=$(POSTGRES_TEST_DSN) \
		$(CURDIR)/$(BACKEND_PY)/python -m pytest -q tests/test_persistence_postgres.py
	@docker rm -f $(PG_TEST_CONTAINER) >/dev/null

check: lint typecheck test

# Parser golden-set eval — real LLM, run on demand / nightly (NOT in CI).
# Honors LLM_PROVIDER / ANTHROPIC_API_KEY from the environment.
eval:
	cd backend && $(CURDIR)/$(BACKEND_PY)/python evals/run_parser_eval.py

# Load test (#10). Point at a server started in stub mode so this measures the
# app's own overhead, not model inference:
#   LLM_PROVIDER=stub RATE_LIMIT_ENABLED=false MAX_CONCURRENT_SCREENINGS=64 \
#     backend/.venv/bin/uvicorn app.main:app --port 8000   (run from backend/)
# Then, in another shell: `make loadtest`. Results + baselines: docs/performance.md.
# Requires the loadtest extra: pip install -e "backend/.[loadtest]".
loadtest:
	$(BACKEND_PY)/locust -f loadtest/locustfile.py --host $(LOADTEST_HOST) \
		--headless --users $(USERS) --spawn-rate $(SPAWN_RATE) --run-time $(RUN_TIME)

# Same test with the live web dashboard (http://localhost:8089) for exploring.
loadtest-ui:
	$(BACKEND_PY)/locust -f loadtest/locustfile.py --host $(LOADTEST_HOST)
