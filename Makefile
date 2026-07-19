# Single entry points so humans and CI run identical commands.
BACKEND_PY := backend/.venv/bin

# Load-test knobs (#10) — override on the CLI, e.g. `make loadtest USERS=200`.
LOADTEST_HOST  ?= http://localhost:8000
USERS          ?= 50
SPAWN_RATE     ?= 10
RUN_TIME       ?= 5m

.PHONY: lint format typecheck test check eval loadtest loadtest-ui

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
