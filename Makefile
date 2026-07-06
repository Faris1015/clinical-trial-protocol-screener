# Single entry points so humans and CI run identical commands.
BACKEND_PY := backend/.venv/bin

.PHONY: lint format typecheck test check

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
