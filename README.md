# Multi-Agent Clinical Trial Protocol Screener

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

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m app.data.generate_ehr          # generate synthetic patients
uvicorn app.main:app --reload --port 8000
```

Requires [Ollama](https://ollama.com) running locally with `ollama pull llama3.1:8b`,
or set `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic`.

### Frontend

```bash
cd frontend
npm install
npm run dev                              # http://localhost:5173
```

### Run a screening

```bash
curl -X POST http://localhost:8000/api/screenings -F "file=@protocol.pdf"
curl -N http://localhost:8000/api/screenings/<thread_id>/stream
curl -X POST http://localhost:8000/api/screenings/<thread_id>/approve
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

## Roadmap

- [x] Repo scaffold: state, schemas, agent nodes, graph wiring, rules DB
- [ ] Parser golden-set eval against real ClinicalTrials.gov eligibility sections
- [ ] Critic LLM semantic-review layer
- [ ] React pipeline visualization with Critic-rejection loop animation
- [ ] Per-criterion patient match breakdown UI
- [ ] Docker compose for one-command demo
