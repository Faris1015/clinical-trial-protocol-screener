# Multi-Agent Clinical Trial Protocol Screener

[![CI](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/ci.yml)

A multi-agent AI system that ingests clinical trial protocols (PDF or markdown), extracts
eligibility criteria into a strict typed schema, cross-checks them against an FDA-style
compliance rules database, and deterministically matches them against a synthetic patient
EHR — with a human-in-the-loop approval gate before any patient data is touched.

> **Disclaimer:** This project uses fully synthetic patient data and simplified compliance
> rules. It is a demonstration of enterprise multi-agent orchestration patterns, not a
> medical device or regulatory tool.

## Why this architecture

A pure-LLM pipeline can't be audited; a pure-rules pipeline can't read prose. This system
uses LLMs **only where language understanding is required**, wraps them in deterministic
validation and typed contracts, and pauses for a human at exactly the point where patient
data gets touched.

## Architecture

```
       [ Protocol Upload (PDF / markdown) ]
                      │
                      ▼
         ┌─────────────────────────┐
         │  Agent 1: Router        │  validates input, extracts eligibility section
         └────────────┬────────────┘
                      ▼
         ┌─────────────────────────┐
    ┌───▶│  Agent 2: Parser        │  LLM + forced JSON schema → typed criteria
    │    └────────────┬────────────┘
    │                 ▼
    │    ┌─────────────────────────┐
    └────┤  Agent 3: Critic        │  deterministic rule checks + LLM semantic review
 rejected└────────────┬────────────┘  (max 3 attempts, then human escalation)
 w/ feedback          ▼ approved
         ═══ HUMAN-IN-THE-LOOP GATE ═══  graph interrupts; human reviews criteria
                      ▼ approved
         ┌─────────────────────────┐
         │  Agent 4: Matcher       │  pure-Python comparison vs synthetic EHR
         └────────────┬────────────┘
                      ▼
         [ React dashboard: live agent execution, criteria provenance, match results ]
```

### Key design decisions

- **Typed criteria, not string lists.** The Parser emits `QuantitativeCriterion`
  (attribute / operator / value / unit) and `CategoricalCriterion` objects with a closed
  attribute vocabulary — the contract that lets the Matcher run as pure Python instead of
  per-patient LLM calls.
- **Provenance on every criterion.** Each extracted criterion carries the verbatim
  `source_text` from the protocol so reviewers can audit every threshold.
- **The Parser is allowed to admit defeat.** Vague criteria ("adequate organ function")
  go into an explicit `unparseable` bucket instead of being hallucinated into numbers.
- **The Critic is hybrid.** Layer 1 is a deterministic YAML rules database (testable,
  auditable); layer 2 is an LLM semantic review for contradictions rules can't catch.
- **Self-correcting loop with a hard cap.** Critic rejections route back to the Parser
  with structured feedback; after 3 failed attempts the graph terminates at a
  `human_escalation` node instead of looping forever.
- **Human-in-the-loop at the right place.** The graph compiles with
  `interrupt_before=["matcher"]` — a human approves the parsed criteria before patient
  matching runs.
- **Append-only event log in graph state** (`Annotated[list, operator.add]` reducer)
  powers the frontend's live "which agent owns the token" visualization for free.

## Tech stack

| Layer | Choice |
|---|---|
| Orchestration | **LangGraph** (StateGraph, checkpointer, conditional edges, interrupts) |
| API | **FastAPI** with SSE streaming of graph events |
| Validation | **Pydantic v2** — schemas double as LLM structured-output contracts |
| LLM | **Ollama** (`llama3.1:8b`) locally, or hosted Claude via the same interface |
| Frontend | **React + TypeScript + Vite** — live pipeline visualization |
| Synthetic data | Seeded Faker-based EHR generator (reproducible demos) |

## Quickstart

### Docker (recommended)

One command brings up the whole stack — backend, frontend, and a local Ollama
that pulls its model on first run:

```bash
docker compose up --build
```

Then open **http://localhost:8080**. On the first run Ollama downloads
`llama3.1:8b` (~4.7GB) before the backend starts — subsequent runs reuse the
cached model volume. Synthetic patients are generated automatically into a
data volume on first start. `depends_on` health conditions order startup so the
frontend only comes up once the backend is healthy; check with
`docker compose ps`.

To use hosted Claude instead of local Ollama, create a root `.env`:

```bash
echo "LLM_PROVIDER=anthropic"       >> .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
docker compose up --build
```

### Manual (local dev)

<details>
<summary>Run without Docker</summary>

#### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m app.data.generate_ehr          # generate synthetic patients
uvicorn app.main:app --reload --port 8000
```

Requires [Ollama](https://ollama.com) running locally with `ollama pull llama3.1:8b`,
or set `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic`.

#### Frontend

```bash
cd frontend
npm install
npm run dev                              # http://localhost:5173
```

</details>

### Run a screening

```bash
curl -X POST http://localhost:8000/api/screenings -F "file=@protocol.pdf"
curl -N http://localhost:8000/api/screenings/<thread_id>/stream
curl -X POST http://localhost:8000/api/screenings/<thread_id>/approve
```

## Code quality

Backend is linted and formatted with **ruff** and type-checked with **mypy**
(strict `disallow_untyped_defs` on `app/`); frontend uses **ESLint**
(typescript-eslint + react-hooks), **Prettier**, and strict TypeScript with
zero `any`. Shared API payload types live in
[`frontend/src/types.ts`](frontend/src/types.ts), mirroring the backend
Pydantic schemas.

```bash
make lint        # ruff + eslint
make format      # ruff format + prettier
make typecheck   # mypy + tsc --noEmit
make test        # pytest
make check       # all of the above — what CI runs
```

Install the git hooks once and every commit runs the same checks on staged
files:

```bash
pip install pre-commit && pre-commit install
```

## Project structure

```
backend/
  app/
    main.py                    # FastAPI app + SSE endpoints
    graph/
      state.py                 # Shared LangGraph state (typed, with event reducer)
      builder.py               # Graph assembly: nodes, edges, loop, HITL interrupt
      nodes/                   # router / parser / critic / matcher
    schemas/criteria.py        # Pydantic criteria contracts
    rules/compliance_rules.yaml# Deterministic FDA-style boundary rules
    data/generate_ehr.py       # Seeded synthetic patient generator
    services/                  # LLM factory, PDF eligibility-section extraction
  tests/
frontend/
  src/
    hooks/useScreenerStream.ts # SSE consumption of graph events
    components/                # PipelineGraph, AgentCard, CriteriaTable, matches
```

## Production roadmap

The scaffold works end-to-end; the path to production-grade is tracked as GitHub issues,
organized around four pillars. Each issue carries acceptance criteria and a priority label
(`P1` = do first / blocks other work, `P2` = core production requirement, `P3` = hardening).

### 1. Architectural foundations — `architecture`

| Issue | Priority | What it delivers |
|---|---|---|
| [#1 Centralized configuration](../../issues/1) | P1 | pydantic-settings, `.env.example`, zero hardcoded values |
| [#2 Durable state persistence](../../issues/2) | P1 | SQLite/Postgres checkpointer — screenings survive restarts and scale past one replica |
| [#4 Defensive error handling](../../issues/4) | P1 | Exception hierarchy, exponential backoff on LLM calls, graceful SSE error events |
| [#3 Service-layer separation](../../issues/3) | P2 | Routes → services → graph → nodes; no business logic in handlers |
| [#15 API hardening](../../issues/15) | P2 | Upload limits, rate limiting, concurrency caps, SSE hygiene |
| [#16 Complete stubbed intelligence](../../issues/16) | P2 | Critic LLM semantic review + Matcher semantic term-mapping (fixes the substring pitfall) |

### 2. Operational visibility — `observability`

| Issue | Priority | What it delivers |
|---|---|---|
| [#5 Structured logging](../../issues/5) | P1 | JSON logs with `thread_id`/`request_id` correlation, PHI-safe by construction |
| [#6 Health & readiness endpoints](../../issues/6) | P2 | `/health` liveness + `/ready` dependency checks (LLM, rules, data, DB) |
| [#7 Metrics & telemetry](../../issues/7) | P3 | Prometheus metrics per agent node, Grafana dashboard, critic-rejection rates |

### 3. Rigorous testing & QA — `testing`

| Issue | Priority | What it delivers |
|---|---|---|
| [#8 Linting & typing](../../issues/8) | P1 | ruff + mypy + ESLint/Prettier + pre-commit; no `any`, no bare excepts |
| [#9 Test coverage expansion](../../issues/9) | P2 | FakeChatModel integration tests, loop-convergence tests, Parser golden-set eval, 80% gate |
| [#10 Load testing](../../issues/10) | P3 | Locust SSE fan-out benchmarks, documented performance baseline |

### 4. Deployment pipeline (CI/CD) — `ci-cd`

| Issue | Priority | What it delivers |
|---|---|---|
| [#11 Containerization](../../issues/11) | P1 | Multi-stage Dockerfiles, `docker compose up` one-command stack, pinned deps |
| [#12 CI with GitHub Actions](../../issues/12) | P1 | Lint + typecheck + tests + docker build on every PR; branch protection |
| [#13 CD with zero-downtime rollout](../../issues/13) | P2 | GHCR images on merge, deploy with `/ready`-gated rolling updates, smoke tests, rollback |
| [#14 Version control workflow](../../issues/14) | P2 | CONTRIBUTING, PR/issue templates, CODEOWNERS, conventional commits, squash-merge |

### Suggested execution order

```
Phase 1 (unblock everything):  #1 config → #8 lint/type → #12 CI → #14 workflow
Phase 2 (make it robust):      #4 errors → #5 logging → #2 persistence → #9 tests
Phase 3 (make it shippable):   #11 docker → #6 health → #13 CD
Phase 4 (make it excellent):   #15 hardening → #16 intelligence → #7 metrics → #10 load
```

Phase 1 first because every later PR then lands through CI with lint/type/test gates —
the guardrails pay for themselves on all subsequent work.

## Development workflow

The full flow — local setup, branching, PRs, conventional commits, and repo
settings — lives in [`CONTRIBUTING.md`](CONTRIBUTING.md). In short:

1. Pick an issue, branch from `main`: `feat/<issue>-<slug>` or `fix/<issue>-<slug>`
2. Open a PR referencing the issue (`Closes #N`) — CI must pass (lint, types, tests, build)
3. Squash-merge with a conventional-commit title (`feat:`, `fix:`, `test:`, `docs:`, `chore:`)
4. Merge to `main` triggers CD: image build → registry → rolling deploy gated on `/ready`

### CI

Every PR and push to `main` runs [`ci.yml`](.github/workflows/ci.yml): parallel backend
(ruff, mypy, pytest with a ratcheting coverage gate) and frontend (eslint, prettier, tsc,
vite build) jobs. A [`pr-title.yml`](.github/workflows/pr-title.yml) check enforces a
Conventional Commits PR title (the squash-merge commit message).
[`docker.yml`](.github/workflows/docker.yml) rebuilds images only when
container files or dependency manifests change. Superseded runs on the same ref are
cancelled automatically. Branch protection and merge settings are documented in
[`CONTRIBUTING.md`](CONTRIBUTING.md#repository-settings).

## Configuration

All runtime configuration is environment-driven via `app/config.py`
(pydantic-settings). Copy [`backend/.env.example`](backend/.env.example) to
`backend/.env` for local development — it is the authoritative variable list.
Validation runs at startup: a misconfigured deployment (e.g.
`LLM_PROVIDER=anthropic` without a key, or a missing rules file) fails fast
with a clear message instead of erroring mid-screening.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` (local) or `anthropic` (hosted) |
| `OLLAMA_MODEL` | `llama3.1:8b` | Local model tag |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server address |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Hosted model id |
| `ANTHROPIC_API_KEY` | — | **Required** when `LLM_PROVIDER=anthropic` |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature (0–1) |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed origins |
| `MAX_PARSE_ATTEMPTS` | `3` | Parser retries before human escalation (1–10) |
| `RULES_PATH` | `app/rules/compliance_rules.yaml` | Compliance rules database |
| `PATIENTS_PATH` | `app/data/patients.json` | Synthetic EHR location |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
