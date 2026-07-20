# Multi-Agent Clinical Trial Protocol Screener

[![CI](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/ci.yml)
[![CD](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/cd.yml/badge.svg?branch=main)](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/cd.yml)

**Deploy your own demo:** a one-container, zero-cost public demo — the React SPA
and API served from a single image in stub-LLM mode, no credit card or API key —
deploys to Render or a Hugging Face Space in a few clicks. See
[Free demo deploy](docs/free-demo-deploy.md). To run the full stack locally,
`docker compose up` (below).

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
- **Layered by responsibility: routes → services → graph → nodes.** Route handlers
  in `main.py` only translate between HTTP and the service layer — they read the
  request, resolve the wired dependencies (store, graph), and hand off. All
  screening business logic (input parsing, state construction, graph invocation,
  status denormalization, SSE framing) lives in `app/services/screening.py`; the
  SSE wire format lives in the single-purpose `app/services/sse.py`. The dependency
  arrow points one way — **nodes never import FastAPI**, and `main.py` never imports
  the graph builder except through the service layer. Each module states its one
  responsibility in its docstring, and service functions are unit-tested directly
  without a running server.

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

### One-container demo (stub LLM, no model)

For a zero-dependency spin-up — no Ollama, no API key, no second container — the
[`deploy/demo/Dockerfile`](deploy/demo/Dockerfile) builds the SPA into the backend
image and serves both from one origin in `LLM_PROVIDER=stub` mode (deterministic,
canned extractions; the full pipeline still runs end-to-end):

```bash
docker build -f deploy/demo/Dockerfile -t screener-demo .
docker run --rm -p 8000:8000 screener-demo   # open http://localhost:8000
```

This is the image behind the free public demo — see
[`docs/free-demo-deploy.md`](docs/free-demo-deploy.md) to host it on Render or a
Hugging Face Space for free.

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
curl http://localhost:8000/api/screenings        # list all screenings (newest first)
```

State is durable: a screening parked at the human-approval gate survives a
server restart or deploy and stays resumable (see [Configuration](#configuration)).

### Health & readiness

```bash
curl http://localhost:8000/health   # liveness: 200 whenever the process is up
curl http://localhost:8000/ready    # readiness: 200 only when dependencies are reachable
```

- **`/health`** is dependency-free — it answers "is the process alive?" and
  backs the container `HEALTHCHECK`, so a hung or crashed process is restarted
  without a blipping dependency triggering a restart storm.
- **`/ready`** answers "can this instance serve traffic?" — it checks the LLM
  backend, the compliance rules, the patient EHR, and the screening store
  concurrently (each under a timeout), returning `200` when all pass or `503`
  with a per-check breakdown otherwise. Point load balancers and
  `kubelet` readiness probes here. Both responses include the build `version`
  and `commit`.

### Metrics & telemetry

Prometheus metrics are exposed at `GET /metrics` (standard HTTP metrics via
[`prometheus-fastapi-instrumentator`](https://github.com/trallnag/prometheus-fastapi-instrumentator)
plus the custom domain metrics below). Set `METRICS_ENABLED=false` to unmount
the endpoint.

```bash
curl http://localhost:8000/metrics
```

Bring up Prometheus + Grafana with a pre-provisioned dashboard alongside the
main stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up
#   Prometheus  http://localhost:9090
#   Grafana     http://localhost:3000   ("Protocol Screener — Pipeline" dashboard)
```

Run a screening (upload → stream → approve) and the dashboard renders the funnel,
node latencies, Critic rejection rates, and LLM latency end-to-end.

**Custom metrics** — the questions HTTP timings alone can't answer:

| Metric | Type | Labels | What it answers |
|---|---|---|---|
| `screenings_total` | counter | `outcome` (`done`/`failed`/`escalated`) | Pipeline funnel — how runs end |
| `agent_node_duration_seconds` | histogram | `agent` (`router`/`parser`/`critic`/`matcher`/`human_escalation`) | Per-node latency; p95 screening duration |
| `critic_rejections_total` | counter | `rule_id` (e.g. `HEPATIC-001`, `LLM-SEM`) | Which compliance rules actually fire |
| `parse_attempts` | histogram | — | How deep the self-correction loop runs per screening |
| `llm_call_duration_seconds` | histogram | `provider` (`ollama`/`anthropic`) | LLM call latency distribution |
| `llm_call_failures_total` | counter | `provider` | LLM calls that exhausted retries |

Nodes are instrumented through the graph's `_instrument` decorator and LLM calls
through the single `invoke_with_retry` door, so agent bodies stay free of metrics
plumbing. Definitions live in one place — [`app/services/metrics.py`](backend/app/services/metrics.py).

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

### Testing

The backend suite runs fully offline in seconds — the LLM is faked, so there is
no network or GPU dependency and CI stays deterministic. Coverage is enforced by
a gate in [`pyproject.toml`](backend/pyproject.toml)
(`[tool.coverage.report] fail_under = 80`, the seeded EHR generator omitted) so
the floor is one source of truth for both `make test` and CI.

- **Unit** — Matcher boundaries, deterministic Critic rules, SSE framing, retry
  policy, data-store guards (each pure component tested in isolation).
- **Service** — the screening use-cases (create/stream/approve/state) driven
  directly against an in-memory store with fake graphs.
- **Integration** — the *real* compiled graph with an in-memory checkpointer and
  a scripted `FakeChatModel`: the Critic→Parser loop converges, the escalation
  cap trips after `MAX_PARSE_ATTEMPTS`, the Router reject edge is clean, and the
  full upload → stream → interrupt → approve path runs over HTTP via
  `httpx.AsyncClient` + `ASGITransport`.

#### Parser golden-set eval

Extraction quality (the one non-deterministic node) is gauged separately by a
hand-labeled eval — real LLM, **run on demand / nightly, not in the CI gate**.
See [`backend/evals/`](backend/evals/README.md).

```bash
make eval    # LLM_PROVIDER + ANTHROPIC_API_KEY honored from the environment
```

The set mixes two origins (69 labeled criteria across 9 protocols), scored
separately and combined:

- **Curated** (5 protocols, 31 criteria) — written for this repo inside the
  `EhrAttribute` vocabulary; measures quality on the happy path.
- **Real** (4 protocols, 38 criteria) — verbatim eligibility sections from
  public ClinicalTrials.gov records (NCT ids + access dates in
  [`sources.json`](backend/evals/sources.json)); deliberately messy, measures
  robustness on production-shaped input.

Matching is **semantic and functional** — it scores what changes a screening
decision, not string form. The `category` enum is reported as a separate
diagnostic because the Matcher never reads it. Real sections are hand-labeled
under a documented convention (in-vocab numerics → quantitative; concrete terms
→ categorical; unrepresentable medical criteria → `unparseable`; administrative
text → omitted). Details in [`backend/evals/`](backend/evals/README.md).

**Baseline** — `llama3.1:8b` via Ollama (`make eval`; local 8B model, no
fine-tuning). The two origins are read differently, on purpose:

*Curated set — labels are exhaustive, so precision **and** recall are both
meaningful:*

| Criterion type | Gold | Precision | Recall |
|---|---|---|---|
| inclusion_quantitative | 17 | 0.67 | 0.71 |
| inclusion_categorical | 4 | 0.80 | 1.00 |
| exclusion_quantitative | 2 | 1.00 | 1.00 |
| exclusion_categorical | 7 | 0.22 | 0.29 |
| unparseable | 1 | 1.00 | 1.00 |
| **overall** | **31** | **0.60** | **0.68** |

*Real set (ClinicalTrials.gov) — labels are a curated subset, so **read recall**;
precision is a confounded floor (the model is charged a false positive for every
real criterion we deliberately omitted — e.g. 21 administrative items it dumped
into `unparseable`), not a fair metric:*

| Criterion type | Gold | Recall |
|---|---|---|
| inclusion_quantitative | 4 | 0.25 |
| inclusion_categorical | 7 | 0.29 |
| exclusion_quantitative | 3 | 0.33 |
| exclusion_categorical | 16 | 0.06 |
| unparseable | 8 | 0.00 |
| **overall** | **38** | **0.13** |

Category-label accuracy (diagnostic, not in P/R): **0.78** (7/9 matched
categoricals).

**Reading.** On clean, in-vocabulary input the 8B model is dependable at exactly
what the deterministic Critic leans on — numeric thresholds (`exclusion_quantitative`
1.00/1.00) and routing vague criteria to `unparseable`. Its weak spot is
categorical exclusions (over-extraction + inclusion/exclusion bucket confusion,
not category mislabeling). On raw ClinicalTrials.gov text recall collapses to
~0.13: a small local model under-extracts the criteria that matter from long,
messy protocols. That gap — clean vs. real — is the headline finding, and the
concrete case for a larger model and the LLM semantic-review layer the Critic
currently stubs (`run_llm_semantic_review`). Numbers move with the model; treat
them as a snapshot, not a contract. Reproduce with `make eval` (prints curated,
real, and combined tables).

#### Load testing

Concurrent-load behaviour is measured with **Locust** driving the full reviewer
journey (upload → hold SSE → approve → results) against a server in
**stub-LLM mode** (`LLM_PROVIDER=stub`), which isolates app overhead from model
latency.

```bash
docker compose -f docker-compose.loadtest.yml up --build   # backend, no Ollama
make loadtest                                              # 50-user, 5-min run
```

A single instance sustains **50 concurrent screenings at p95 ≈ 12–21 ms with
< 0.5 % errors and no memory growth over a 5-minute soak**. The load test also
found and fixed a SQLite write-lock bug that failed ~97 % of uploads under
concurrency. Full method, numbers, and analysis:
[`docs/performance.md`](docs/performance.md) ·
[`loadtest/README.md`](loadtest/README.md).

## Project structure

```
backend/
  app/
    main.py                    # FastAPI app: thin HTTP routes → service layer
    graph/
      state.py                 # Shared LangGraph state (typed, with event reducer)
      builder.py               # Graph assembly: nodes, edges, loop, HITL interrupt
      nodes/                   # router / parser / critic / matcher
    schemas/criteria.py        # Pydantic criteria contracts
    rules/compliance_rules.yaml# Deterministic FDA-style boundary rules
    data/generate_ehr.py       # Seeded synthetic patient generator
    services/
      screening.py             # Screening use-cases (create/stream/approve/state)
      sse.py                   # Server-Sent Events wire format (one place)
      llm.py, pdf.py           # LLM factory, PDF eligibility-section extraction
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
4. Merge to `main` triggers CD: backend + frontend images built and pushed to GHCR; the hosting platform auto-deploys from `main` (see [Free demo deploy](docs/free-demo-deploy.md))

### CI

Every PR and push to `main` runs [`ci.yml`](.github/workflows/ci.yml): parallel backend
(ruff, mypy, pytest with a ratcheting coverage gate) and frontend (eslint, prettier, tsc,
vite build) jobs. A [`pr-title.yml`](.github/workflows/pr-title.yml) check enforces a
Conventional Commits PR title (the squash-merge commit message).
[`docker.yml`](.github/workflows/docker.yml) rebuilds images only when
container files or dependency manifests change. Superseded runs on the same ref are
cancelled automatically. Branch protection and merge settings are documented in
[`CONTRIBUTING.md`](CONTRIBUTING.md#repository-settings).

### CD

Merge to `main` triggers [`cd.yml`](.github/workflows/cd.yml): it builds and
pushes the backend + frontend images to GHCR, each tagged with the commit SHA and
`latest`, and bakes `GIT_SHA` into the backend so [`/health`](#health--readiness)
and `/ready` report exactly which build is live. Images only build when relevant
files change, and runs on `main` are serialized so two quick merges can't race the
`:latest` tag.

**Deployment itself is delegated to the hosting platform's own auto-deploy from
the repo** — the free public demo rebuilds the single-container image straight
from `main` on every push (Render blueprint via [`render.yaml`](render.yaml), or a
Hugging Face Space). One-command setup for either is in
[`docs/free-demo-deploy.md`](docs/free-demo-deploy.md).

For a real deployment, [`docs/deployment.md`](docs/deployment.md) documents a full
production topology — separate backend and nginx-frontend containers, a Postgres
checkpointer, and dedicated LLM inference — with a [`/ready`](#health--readiness)-gated
**zero-downtime rolling update** and a **rollback** procedure.

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
| `CHECKPOINT_BACKEND` | `sqlite` | `memory` (tests), `sqlite` (durable single-node), `postgres` (multi-replica) |
| `SQLITE_PATH` | `screenings.sqlite` | sqlite file shared by the checkpointer and screening store |
| `POSTGRES_DSN` | — | **Required** when `CHECKPOINT_BACKEND=postgres`; install with `pip install -e ".[postgres]"` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `console` | `console` (human-readable) or `json` (one object per line) |
