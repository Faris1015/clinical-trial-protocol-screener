# Single entry points so humans and CI run identical commands.
BACKEND_PY := backend/.venv/bin

.PHONY: lint format typecheck test check eval

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
