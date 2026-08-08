# TrialGate

**Multi-agent clinical trial protocol screening, with a human at the gate.**

[![CI](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/ci.yml)
[![CD](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/cd.yml/badge.svg?branch=main)](https://github.com/Faris1015/clinical-trial-protocol-screener/actions/workflows/cd.yml)

> The product is named **TrialGate**; the GitHub repository slug remains
> `clinical-trial-protocol-screener`, so clone URLs, badges, and existing links
> are unchanged.

**Deploy your own demo:** a one-container, zero-cost public demo — the Next.js
frontend and API served from a single image in stub-LLM mode, no credit card or
API key — deploys to Render or a Hugging Face Space in a few clicks. See
[Free demo deploy](docs/free-demo-deploy.md). To run the full stack locally,
`docker compose up` (below).

TrialGate is a multi-agent AI system that ingests clinical trial protocols (PDF or
markdown), extracts eligibility criteria into a strict typed schema, cross-checks them
against an FDA-style compliance rules database, and deterministically matches them against
a synthetic patient EHR — with a human-in-the-loop approval gate before any patient data
is touched.

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
        [ Next.js dashboard: live agent execution, criteria provenance, match results ]
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
- **The human can fix things, not just wave them through.** A reviewer edits the
  criteria at the gate (or after an escalation) and the corrected extraction is
  written into the checkpoint *as the Parser* — so the Critic re-runs over it and
  the run re-parks for approval. Edits can't bypass compliance, and matching still
  needs a named approver. Every revision keeps its before/after diff in state.
- **The human can also say no.** Rejection is a first-class, audited decision
  symmetric with approval: a required reason plus `rejected_by`/`rejected_at` land
  in the checkpoint before the graph terminates, so a protocol nobody can screen
  ends as a named decision rather than a run parked at the gate forever.
- **Every result is dual-layer.** The Critic's findings and the Matcher's
  per-patient verdicts each carry a plain-language `explanation` next to the
  technical wording, plus a one-line `summary` per result ("Alice matches — meets
  all 6 inclusion criteria; the one exclusion (prior chemo) does not apply"). The
  UI defaults to plain language with a **Plain language / Technical** toggle one
  click away, and the rule id stays on screen in both — a readable finding that
  dropped its provenance would not be auditable. The plain layer is rendered from
  the same deterministic comparison as the statuses, never a second LLM opinion,
  so it cannot contradict them.
- **The cohort explains itself.** Because the Matcher's verdicts are per-criterion
  and deterministic, "what screened this cohort out" is a pure reduction over the
  checkpoint rather than a new pipeline: criteria ranked by exclusions, with the
  overlap between them reported so relaxing the top one cannot promise a delta the
  cohort will not pay.
- **The gate has a name attached.** Clearing it requires an authenticated
  reviewer, and the approver's identity is written into the checkpoint
  (`approved_by`, `approved_by_role`, `approved_at`, plus an event-log entry)
  *before* the matcher resumes — so the authorization to touch patient data is
  durable even if matching then fails. Auth lives in the backend rather than
  NextAuth because the frontend is a static export with no request-time server;
  [`app/auth.py`](backend/app/auth.py) has the full reasoning.
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
| LLM | **Ollama** (`qwen2.5:7b`) locally, or hosted Claude via the same interface |
| Frontend | **Next.js (App Router) + TypeScript** — static export, live pipeline visualization |
| UI | **Tailwind v4 + shadcn/ui** — dashboard shell, light/dark themes, kit documented at `/design` |
| Synthetic data | Seeded Faker-based EHR generator (reproducible demos) |

## Quickstart

### Docker (recommended)

One command brings up the whole stack — backend, frontend, and a local Ollama
that pulls its model on first run:

```bash
docker compose up --build
```

Then open **http://localhost:8080** and sign in with a demo account (below). On
the first run Ollama downloads
`qwen2.5:7b` (~4.7GB) before the backend starts — subsequent runs reuse the
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
[`deploy/demo/Dockerfile`](deploy/demo/Dockerfile) builds the frontend into the
backend image and serves both from one origin in `LLM_PROVIDER=stub` mode
(deterministic, canned extractions; the full pipeline still runs end-to-end):

```bash
docker build -f deploy/demo/Dockerfile -t trialgate-demo .
docker run --rm -p 8000:8000 trialgate-demo   # open http://localhost:8000
```

Sign in with a demo account (see [Authentication & roles](#authentication--roles)).

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

Requires [Ollama](https://ollama.com) running locally with `ollama pull qwen2.5:7b`,
or set `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic`.

#### Frontend

```bash
cd frontend
npm install
npm run dev                              # http://localhost:3000
```

`next dev` proxies `/api/*` to `http://localhost:8000` (override with
`NEXT_DEV_API_UPSTREAM`), so the browser talks to one origin in dev exactly as it
does in the deployed images. `npm run build` produces a static export in
`frontend/out` — see [`next.config.ts`](frontend/next.config.ts) for why, and for
what changes if a route ever needs request-time rendering.

</details>

### Authentication & roles

The human-in-the-loop gate is the point where patient data gets touched, so it is
gated to authorized reviewers — and the approver's identity is recorded in the
run's audit trail. Two roles:

| Role | Can |
|---|---|
| **reviewer** | Upload protocols, review criteria, and clear the approval gate |
| **admin** | Everything a reviewer can, plus manage accounts and compliance rules |

Out of the box (no `AUTH_USERS` configured) two demo accounts are seeded:

| Email | Password | Role |
|---|---|---|
| `reviewer@trialgate.local` | `trialgate-reviewer` | reviewer |
| `admin@trialgate.local` | `trialgate-admin` | admin |

> **These are published demo credentials for the synthetic-data demo.** For any
> real deployment set `AUTH_SECRET` and `AUTH_USERS`, and
> `AUTH_DEMO_USERS=false`. Provision an account by minting a hash:
>
> ```bash
> cd backend && python -m app.auth hash        # prompts, prints an scrypt hash
> # then: AUTH_USERS=jane@example.com:reviewer:scrypt$16384$8$1$...
> ```

Unauthenticated browsers are redirected to `/login`; every `/api` route answers
`401` without a session and `403` when the role doesn't cover the action.
Sessions are signed, expiring tokens in an httpOnly `SameSite=Strict` cookie —
[`backend/app/auth.py`](backend/app/auth.py) documents why the backend owns auth
rather than NextAuth (the frontend is a static export with no request-time
server), and how CSRF is handled.

### Run a screening

```bash
# 1. Sign in; -c/-b keep the session cookie in a jar between calls.
curl -X POST http://localhost:8000/api/auth/login -c cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"email":"reviewer@trialgate.local","password":"trialgate-reviewer"}'

# 2. Screen a protocol.
curl -b cookies.txt -X POST http://localhost:8000/api/screenings -F "file=@protocol.pdf"
curl -b cookies.txt -N http://localhost:8000/api/screenings/<thread_id>/stream
curl -b cookies.txt -X POST http://localhost:8000/api/screenings/<thread_id>/approve

# 3. Past runs: one page at a time, newest first. Returns
#    {items, total, limit, offset} — `total` is what matched the filter, so it
#    is how you know whether there is a next page.
curl -b cookies.txt http://localhost:8000/api/screenings
curl -b cookies.txt 'http://localhost:8000/api/screenings?limit=50&offset=50'
curl -b cookies.txt 'http://localhost:8000/api/screenings?status=awaiting_approval'
curl -b cookies.txt 'http://localhost:8000/api/screenings?q=nsclc'   # filename or thread_id
```

`limit` defaults to 25 and caps at 100; `status` accepts the phases a screening
can be in (`routing`, `parsing`, `critiquing`, `awaiting_approval`, `matching`,
`done`, `failed`, `escalated`, `rejected`) and anything else is a 422. Each row carries the
uploaded filename plus the run's `criteria_count` and `match_count`, so the
**Past Runs** page renders the index without loading a checkpoint per screening.
A run's detail view deep-links to `/runs/view/?id=<thread_id>` and rehydrates
read-only from `/state`.

State is durable: a screening parked at the human-approval gate survives a
server restart or deploy and stays resumable (see [Configuration](#configuration)).
After approval, the run's state carries `approved_by`, `approved_by_role`, and
`approved_at`, plus a `human`/`approved` entry in its event log:

```bash
curl -b cookies.txt http://localhost:8000/api/screenings/<thread_id>/state
```

### Screen several protocols at once

A coordinator opening a trial rarely has one protocol. **New Screening** takes a
multi-file selection: each file becomes its own screening thread, and the page
shows one row per protocol with its live phase and a link into the run. There is
no batch entity to navigate afterwards — the runs are ordinary runs, so they land
in **Past Runs**, queue up in **Review Queue**, and export like any other.

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/screenings/batch \
  -F "files=@nsclc.pdf" -F "files=@scan.pdf" -F "files=@crc.md"
# → {"created": 2, "rejected": 1,
#    "items": [{"filename": "nsclc.pdf", "thread_id": "…", "error": null, "detail": null},
#              {"filename": "scan.pdf",  "thread_id": null,
#               "error": "ExtractionError", "detail": "…"},
#              {"filename": "crc.md",    "thread_id": "…", "error": null, "detail": null}]}
```

- **Partial success is the contract.** One scanned PDF in a folder of eight must
  not lose the other seven, so a file the edge refuses (415 wrong type, 413 too
  large, 422 unreadable) is reported as that item's `{error, detail}` — the same
  pair every rejection carries — and the batch still answers 200. A broken *server*
  is not a broken file: a store outage fails the whole submission rather than being
  reported as eight bad protocols. `items` echoes the submission's order.
- **One request, not N.** `POST /api/screenings` is rate limited per request
  (`RATE_LIMIT_CREATE`), so a client looping over ten files would trip the limiter
  partway through its own batch. A submission is one request against that budget,
  bounded instead by `MAX_BATCH_FILES` (10) and, in aggregate, by
  `MAX_UPLOAD_BYTES` × that cap.
- **Uploading is not running.** Like a single upload, each thread executes when a
  client streams it — so the batch view streams two at a time against a gate that
  allows four (`MAX_CONCURRENT_SCREENINGS`), leaving room for someone else's
  screening rather than filling the instance. A row that finds the gate saturated
  waits out the server's `Retry-After` instead of reporting itself failed.
- **Each run stops where a single one does.** The human gate is not batched away:
  every protocol runs to `awaiting_approval` (or escalates), and approving one is
  still a named reviewer's decision on its own run.

### Trace a criterion back to the protocol

Every criterion carries the verbatim sentence it was extracted from — but a
sentence quoted next to a criterion is still the extraction vouching for itself.
On the live screening and on a past run's detail page, the criteria sit beside
the uploaded protocol: **click a criterion and its source passage is highlighted
and scrolled to** in the document. Passages the extraction accounts for are
faintly underlined even when nothing is selected, so it is visible at a glance
how much of the eligibility section was read and how much was skipped.

```bash
curl -b cookies.txt http://localhost:8000/api/screenings/<thread_id>/protocol
# → {"thread_id", "source_filename", "text", "spans": [{"source_text", "start", "end", "exact"}]}
```

The spans are resolved server-side (`app/services/provenance.py`) because the
match cannot be a plain substring search: PDF extraction wraps a sentence across
lines, the Parser strips a folded-in header or list marker before storing it, and
a model occasionally paraphrases the sentence it was told to quote. So the search
runs on a whitespace-collapsed, casefolded projection of both strings, mapped back
to real character offsets. Two outcomes are deliberately visible rather than
smoothed over:

- **Partial match** (`exact: false`) — only the leading run of words could be
  located. The viewer highlights it and says the passage is approximate.
- **No match** — the sentence is absent from `spans` entirely, and the viewer says
  the source could not be found in the protocol, quoting what the extraction
  claims. A confidently wrong highlight is worse for an audit than no highlight,
  so a fallback that would keep fewer than five words is refused.

### Edit and re-run at the gate

The gate is not approve-only. A reviewer looking at a bad threshold, a
hallucinated criterion, or a sentence the Parser dumped into `unparseable` can
correct the extraction and re-run it — which is also the only exit for a run the
Critic escalated. **Review Queue** lists every screening waiting on a person
(`awaiting_approval`, `escalated`, `failed`) and each row opens the editor at
`/review/edit/?id=<thread_id>`, where every field sits beside the verbatim
protocol sentence it came from.

```bash
# Submit the corrected extraction with the revision it was based on. Streams the
# Critic's re-review over SSE, exactly like /approve.
curl -b cookies.txt -N -X PATCH \
  http://localhost:8000/api/screenings/<thread_id>/criteria \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 0, "criteria": { ...full CriteriaSchema... }}'
```

The edits are written into the checkpoint as if the Parser had produced them, so
the **Critic re-runs over them** — a human edit can't smuggle a compliance
violation past the guardrail — and the run then parks at the gate again for a
named approval, because patient matching must never happen without one. Each
revision bumps `criteria_revision` and appends a `criteria_edits` entry holding
the before/after diff and who made it, which is what the editor and the run's
detail view both render. `base_revision` is the optimistic-concurrency token: two
reviewers on the same parked run means the second save gets a 409 rather than
silently discarding the first's corrections. A finished run is not editable (409)
— its cohort was already scored against the criteria it had.

### Reject at the gate

Some protocols cannot be screened however the criteria are worded — the wrong
document, an eligibility section that isn't one, thresholds this cohort has no
data for. Before, a reviewer who reached that conclusion could only walk away,
leaving the run parked in `awaiting_approval` forever and counted as in flight.
Rejection is the gate's third exit, and it is audited exactly like approval:

```bash
curl -b cookies.txt -X POST \
  http://localhost:8000/api/screenings/<thread_id>/reject \
  -H 'Content-Type: application/json' \
  -d '{"reason": "Device protocol — no eligibility criteria this cohort can be screened on."}'
```

The reason is **required** (422 without one): a terminal state with no
explanation is a dead end nobody can audit. `rejected_by`, `rejected_by_role`,
`rejected_at` and `rejected_reason` are written into the checkpoint *before* the
graph terminates — the same ordering `approved_by` uses — along with a
`human`/`rejected` entry in the event log, so the run timeline and the exported
report both show the decision beside the steps that led to it.

Only a run parked at the gate or escalated can be rejected; anything else is a
409, and a rejected run accepts no further approval, rejection, or edit. Unlike
approve and edit-and-rerun this returns JSON rather than an SSE stream and holds
no concurrency slot — it resumes nothing, so the matcher never runs and no
patient data is touched. `rejected` is its own terminal outcome in the metrics
funnel, distinct from `failed`: "we chose not to screen this" and "we could not"
are different answers.

### Notify on gate / escalation

A run that parks at the approval gate — or escalates because the Critic couldn't
converge — stops and waits indefinitely. Neither is an error, so neither shows up
as one: without an outbound signal the only way to find out is to keep reloading
the Review Queue. Set `NOTIFY_ENABLED=true` plus at least one channel and each of
those stops pushes one notification instead.

```bash
# Slack (or Teams, or any endpoint that takes a JSON POST)
NOTIFY_ENABLED=true
NOTIFY_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/xxxx
NOTIFY_BASE_URL=https://trialgate.example.com   # makes the message clickable

# ...or email, over SMTP submission
NOTIFY_ENABLED=true
NOTIFY_EMAIL_TO=reviewers@example.com,oncall@example.com
NOTIFY_EMAIL_FROM=trialgate@example.com
NOTIFY_SMTP_HOST=smtp.example.com
```

Both channels can be on at once; each is dispatched concurrently under
`NOTIFY_TIMEOUT_SECONDS`. Three properties are deliberate:

- **Opt-in, and validated.** Off by default, and `NOTIFY_ENABLED=true` with no
  usable channel fails at startup rather than no-op'ing — a notification setup
  that looks configured while paging nobody is the failure mode worth catching.
- **No PHI.** The payload is an explicit allowlist — run id, status, protocol
  filename, criteria count, link — built from arguments rather than from graph
  state, so nothing from `matched_patients` or the criteria themselves can leak
  into Slack or a mail relay. A recipient learns a run needs attention and has to
  sign in to see anything more.
- **Never fatal.** Delivery is best-effort: a dead webhook or a refused SMTP
  connection is logged and counted (`notifications_total{channel,outcome}`), never
  surfaced — a notification outage must not fail a screening that succeeded.

Only `awaiting_approval` and `escalated` notify. A run that finished or failed on
its own isn't waiting for anybody, and paging on those is how a channel gets
muted.

### Per-run event timeline

Every node appends to an event log in graph state, which read raw is a flat
sequence of sentences. A past run's detail page renders it as an **event
timeline** instead: each step in the order it happened, with the retry rounds
numbered, the Critic's rejections named, the gap since the previous step, and —
for a human step — who did it. Above the entries, the run's shape in one line:
`Ran in 3m 4s · 2 extraction attempts · 1 Critic rejection · 1 reviewer revision
· Authorized by lead@example.com`.

```bash
curl -b cookies.txt http://localhost:8000/api/screenings/<thread_id>/state
# → {"values", "pending", "screening",
#    "timeline": {"entries": [{"label", "outcome", "elapsed", "attempt",
#                              "revision", "actor", ...}], "summary": {...}}}
```

- **Derived, never stored.** `app/services/timeline.py` reads the same `events`
  list the pipeline already writes, so a run checkpointed months ago gains the
  timeline too — and replaying a run never amends it. Retry rounds are counted
  from the Parser's own runs, which reproduces the numbering in its `(attempt N)`
  detail; an edit-and-rerun deliberately spends none, matching the escalation cap.
- **Identity is correlated, not parsed.** The approver comes from `approved_by`
  (the durable trail, [B1](../../issues/50)) and the Nth reviewer edit from the
  Nth `criteria_edits` record ([#53](../../issues/53)) — not from scraping the
  sentence a node happened to log.
- **Rendered once, shown twice.** The labels, attempt numbers and elapsed gaps are
  resolved server-side, so the exported report below prints the same trail rather
  than a second implementation of it.

### Per-criterion cohort attrition

The cohort table answers *who* is eligible. The question a coordinator actually
asks is *what is killing my cohort* — and until now the app had no screen for it.
**What screened this cohort out**, on a run's detail page, ranks every criterion by
how many patients it excluded:

```bash
curl -b cookies.txt http://localhost:8000/api/screenings/<thread_id>/state
# → {"values", "pending", "screening", "timeline",
#    "attrition": {"totals": {"patients": 100, "eligible": 12, "excluded": 79, ...},
#                  "criteria": [{"label": "egfr >= 60 mL/min/1.73m2", "excluded": 41,
#                                "unique": 22, "shared": 19, "recoverable": 20,
#                                "unresolved": 3, "share": 41.0}, ...],
#                  "overlaps": [{"a_label": ..., "b_label": ..., "patients": 19}]}}
```

- **Free, and deterministic.** The Matcher already writes a per-criterion verdict
  for every patient into `matched_patients`, so `app/services/attrition.py` is a
  pure reduction over data the run has already paid for — no LLM call, no extra
  read, and the same numbers for a run checkpointed months ago.
- **No false deltas.** "eGFR ≥ 60 excludes 41" invites the reader to believe
  relaxing it returns 41 patients, when 19 of them also fail ECOG. Every row
  therefore splits its exclusions into `unique` and `shared`, reports
  `recoverable` — how many patients relaxing it would actually make *eligible*,
  which is fewer again when one of them still has a criterion nobody could
  evaluate — and the top criteria carry their pairwise overlap ("19 patients fail
  both"). This is the data model the what-if simulator drives.
- **One rendering of one run.** The eligible / needs-review / ineligible counts
  come from `app/services/cohort.py`, the same module the runs index, the report
  and the comparison read, so the tally here cannot contradict the table under it.
  Patients no criterion was applied to at all are named rather than left as a gap
  the rows silently fail to fill.
- **Every criterion, ranked.** Including the ones that excluded nobody: "age ≥ 18
  excluded 0" is a fact about the protocol, and ties break on the unresolved count
  then the label, so two exports of one run order the criteria identically.

### Download a screening report

A screening is only useful if it can leave the app. **Download report** — on a
run's detail page, and under the cohort on a finished live run — exports the whole
run as one self-contained HTML document: the extracted criteria beside the
verbatim protocol sentence each came from, any reviewer revisions, the Critic's
findings in *both* the plain and the technical layer, who authorized patient
matching, the cohort attrition breakdown above, the full cohort with per-patient
verdicts, and the event timeline.

```bash
curl -b cookies.txt -OJ http://localhost:8000/api/screenings/<thread_id>/report
# → trialgate-report-<protocol>-<run>.html
```

- **Self-contained, and printable.** No stylesheet, font, script, or image is
  referenced — the file renders identically from a mail attachment or an evidence
  folder years later. Print styles (`@page` margins, repeated table headers, no
  break inside a row) mean **Print → Save as PDF** produces the same document,
  which is why there is no PDF renderer (WeasyPrint's native stack, or a headless
  browser) in the images.
- **Both layers, no toggle.** The app lets a reader pick plain or technical (#52);
  a document can't ask, so every finding and verdict carries the plain-language
  sentence *and* the rule id / operator / threshold behind it.
- **Branded, dated, and disclaimed.** Every report is stamped with its generation
  time in UTC and carries the synthetic-data disclaimer at the top and in the
  footer — the one artifact here that can end up somewhere with no app around it.
- **Rendered from the run's own state.** The document is built from the same
  `/state` payload the detail view renders, so an exported report cannot disagree
  with the screen it came from. A screening that was uploaded but never streamed
  has nothing to report and is a 409.

Unlike a notification, this document *does* carry patient data — that is what it
is for. It requires a session, is served as an attachment under a locked-down CSP,
and every string in it is escaped: the criteria and source sentences are
LLM-rewritten uploaded text, so a protocol containing markup must be text in the
report, never markup in a page served from the app's origin.

### Browse the compliance rules

A Critic finding names the rule that fired — `RENAL-001`, `PLT-001` — and until
now that id was unresolvable without opening the repo. **Rules** lists the whole
deterministic rules database: each rule's id, what it actually tests
(`90 ≤ systolic_bp ≤ 200`), whether it blocks a run or only advises, and why it
exists. Every finding's rule id links straight to its row, so "why was my protocol
blocked" is one click from the block itself.

```bash
curl -b cookies.txt http://localhost:8000/api/rules
# → {"source": "compliance_rules.yaml",
#    "rules": [{"id": "BP-001", "condition": "90 ≤ systolic_bp ≤ 200",
#               "severity": "reject", "check_label": "Plausible range",
#               "description": ..., "plain": ..., "keywords": [...]}]}
```

- **It agrees with the engine, by construction.** Severity is read from the same
  `CHECK_SEVERITY` map `run_deterministic_checks` stamps onto a finding, not
  restated — a page claiming "advisory" for a rule that blocks the run would be
  worse than no page, because a reviewer would believe it. A test trips two rules
  for real and compares the finding's severity against the published one.
- **Served, not bundled.** `RULES_PATH` is deployment configuration, so the page
  fetches the rules the running instance is actually enforcing and names the file
  they came from — not whatever was in the repo when the frontend was built.
- **Both layers again.** The rationale renders as the rule's plain-language
  sentence or its regulatory wording, on the same toggle the findings use (#52),
  so a reviewer arriving from a plain finding doesn't hit a wall of citations.
- **The LLM layer is listed too, and labelled.** Semantic findings cite
  `LLM-SEM`, which has no row in the YAML; the listing describes that pass under
  the same id so the link resolves, marked as a model review rather than a fixed
  rule.

Read-only, deliberately. An admin editor is the natural follow-up, but a rules
file the app can rewrite needs a change trail of its own before a compliance
artifact can rest on it.

### Read the metrics in-app

The domain metrics below have been exported since #7, but reading a screening
funnel out of `screenings_total{outcome="escalated"} 3.0` needs a Prometheus and a
dashboard in front of it. **Metrics** answers the three questions a reviewer asks
of the pipeline — where runs end up, which rules block protocols, and how often
the Parser gets it right first time — and stops there.

```bash
curl -b cookies.txt http://localhost:8000/api/metrics/summary
# → {"since": "2026-08-03T18:22:11+00:00", "exported": true,
#    "funnel": {"total": 12, "outcomes": [{"outcome": "done", "label": "Completed",
#                                          "count": 9, "share": 75.0}, ...]},
#    "rejections": {"total": 10, "per_run": 0.83,
#                   "rules": [{"rule_id": "RENAL-001", "count": 4, "share": 40.0,
#                              "layer": "deterministic"}, ...]},
#    "attempts": {"observations": 11, "mean": 1.82, "first_pass_share": 54.5,
#                 "buckets": [{"label": "1", "count": 6, "share": 54.5}, ...]}}
```

- **It cannot disagree with `/metrics`.** Every number is read off the same
  collectors the exposition endpoint serializes — one source read twice, not a
  second count of the same events. Recounting terminal outcomes out of the
  screening store would have been a second implementation of "what counts as done",
  and a summary whose numbers differ from the metrics they summarize is worse than
  no summary, because a reviewer can't tell which one lied. A test drives a real
  run and compares the payload against the parsed exposition, sample by sample.
- **Process-scoped, and it says so.** These counters live in the serving
  process's memory and reset with it, so the epoch they cover travels in the
  payload and the page states it. "9 completed" means something very different on
  a process up for a week.
- **Rendered server-side.** Shares, outcome labels and the de-cumulated histogram
  captions ("1", "6–10", "more than 10") arrive resolved — reading a Prometheus
  histogram is knowledge about the exposition format, and it belongs beside the
  metric definitions rather than in a component.
- **Every rule id links into the rules viewer.** "RENAL-001 blocks more protocols
  than anything else" is one click from what RENAL-001 actually requires.

It complements Grafana rather than replacing it: no time series, no percentiles,
no alerting. Those are what the dashboard below is for.

### Compare two runs

The same protocol screened twice does not have to produce the same run: the Parser
is an LLM, the Critic can push an extraction back, a reviewer can correct it by
hand (#53), and the rules file is deployment configuration that can be amended. So
"we re-ran it — did anything change?" was a question you could only answer by
opening two tabs and reading. Hold two runs on **Past Runs** and choose
**Compare**: the two extractions land side by side with every difference typed, and
under them the cohort's verdicts, patient by patient. Two *different* protocols
compare the same way — an amendment against the version it replaces.

```bash
curl -b cookies.txt "http://localhost:8000/api/screenings/compare?a=$A&b=$B"
# → {"runs": [{"side": "a", "source_filename": "v1.pdf", "status": "done",
#              "criteria_count": 7, "criteria_revision": 0,
#              "cohort": {"eligible": 4, "review": 1, "ineligible": 7, "total": 12}}, ...],
#    "criteria": {"identical": false, "differences": 2,
#                 "totals": {"unchanged": 6, "modified": 1, "added": 1, "removed": 0},
#                 "buckets": [{"bucket": "inclusion_quantitative",
#                              "rows": [{"kind": "modified", "a": "age >= 18 years",
#                                        "b": "age >= 65 years"}, ...]}]},
#    "matches": {"compared": true, "differences": 1,
#                "totals": {"changed": 1, "only_a": 0, "only_b": 0, "same": 11},
#                "patients": [{"patient_id": "PT-7", "name": "…", "kind": "changed",
#                              "a": {"bucket": "eligible", "label": "Eligible"},
#                              "b": {"bucket": "ineligible", "label": "Ineligible"}}, ...]}}
```

- **Criteria are paired by provenance, not by position.** A run that emitted the
  same criteria in a different order changed nothing, and an index-wise diff would
  report every row below a deletion as modified — which is the failure mode that
  makes a diff useless, because then every re-run looks like a rewrite. The pairing
  key and the labels are the *same ones* the reviewer-edit diff uses
  ([`criteria_edits.bucket_entries`](backend/app/services/criteria_edits.py)), so
  the two views cannot disagree about whether two criteria are the same one.
- **It cannot disagree with either run's own page.** Both columns are built from
  the same `GET /state` payload the run detail view and the exported report render
  from — one read path, three renderings — and a test compares each column against
  that endpoint directly.
- **Unchanged rows are kept.** "The other fourteen criteria are the same" is what
  makes the three differences meaningful; only the differences carry a badge and a
  tint, so they are still what the eye lands on.
- **Every difference is stated in words, not only in colour.** A row says
  `modified` / `added` / `removed`, a removal is struck through, and an absent side
  renders an em dash with screen-reader text — the page survives a monochrome
  printout, which is the form an audit trail tends to arrive in.
- **A verdict that moved is the point.** The cohort table pairs patients on their
  EHR id and lists changed verdicts first; "who was eligible" comes from one shared
  rule ([`services/cohort.py`](backend/app/services/cohort.py)), so this view, the
  runs index's match count and the report agree by construction.
- **Empty columns say why.** A run parked at the approval gate has no cohort and a
  run that never streamed has no criteria — both read as the phase they are in,
  never as "nothing was found".

Comparing a run with itself is refused (422) rather than answered with an
all-identical table: it is a mistyped link, and a page confirming that a run
matches itself reads like a working comparison.

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
#   Grafana     http://localhost:3000   ("TrialGate — Pipeline" dashboard)
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
| `notifications_total` | counter | `channel` (`webhook`/`email`), `outcome` (`sent`/`failed`) | Whether gate/escalation notifications are actually landing |

Nodes are instrumented through the graph's `_instrument` decorator and LLM calls
through the single `invoke_with_retry` door, so agent bodies stay free of metrics
plumbing. Definitions live in one place — [`app/services/metrics.py`](backend/app/services/metrics.py).
Recording is unconditional: `METRICS_ENABLED=false` unmounts `/metrics`, but the
counters are still collected, so the [in-app summary](#read-the-metrics-in-app)
above works on an instance nothing is scraping.

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
  policy, data-store guards, the criteria before/after diff (each pure component
  tested in isolation).
- **Service** — the screening use-cases (create/stream/approve/edit/state) driven
  directly against an in-memory store with fake graphs.
- **Integration** — the *real* compiled graph with an in-memory checkpointer and
  a scripted `FakeChatModel`: the Critic→Parser loop converges, the escalation
  cap trips after `MAX_PARSE_ATTEMPTS`, the Router reject edge is clean, and the
  full upload → stream → interrupt → approve path runs over HTTP via
  `httpx.AsyncClient` + `ASGITransport`. Edit-and-rerun is covered here rather
  than only at the service layer on purpose: rewinding a parked checkpoint back to
  the Critic is LangGraph behavior, and only the real graph can prove it works.

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

**Default model** — `qwen2.5:7b` via Ollama (`make eval`; local 7B, no
fine-tuning). Chosen to run within a 16 GB / default-Docker footprint (see
[Configuration](#configuration)). For comparison, reproduce the larger and
smaller models with `OLLAMA_MODEL=qwen2.5:14b make eval` or
`OLLAMA_MODEL=llama3.1:8b make eval`. The two origins are read differently, on
purpose:

*Curated set — labels are exhaustive, so precision **and** recall are both
meaningful:*

| Criterion type | Gold | Precision | Recall |
|---|---|---|---|
| inclusion_quantitative | 17 | 0.80 | 0.71 |
| inclusion_categorical | 4 | 0.50 | 0.75 |
| exclusion_quantitative | 2 | 1.00 | 1.00 |
| exclusion_categorical | 7 | 0.62 | 0.71 |
| unparseable | 1 | 1.00 | 0.00 |
| **overall** | **31** | **0.71** | **0.71** |

*Real set (ClinicalTrials.gov) — labels are a curated subset, so **read recall**;
precision is a confounded floor (the model is charged a false positive for every
real criterion we deliberately did not label), not a fair metric:*

| Criterion type | Gold | Recall |
|---|---|---|
| inclusion_quantitative | 4 | 0.50 |
| inclusion_categorical | 7 | 0.43 |
| exclusion_quantitative | 3 | 0.33 |
| exclusion_categorical | 16 | 0.50 |
| unparseable | 8 | 0.00 |
| **overall** | **38** | **0.37** |

Category-label accuracy (diagnostic, not in P/R): **0.47** (9/19 matched
categoricals).

**Reading.** On the **curated** set `qwen2.5:7b` is the strongest of the three
models tried — overall **0.71/0.71**, ahead of both `llama3.1:8b` (0.60/0.68) and
`qwen2.5:14b` (0.58/0.61). Numeric extraction is solid (`exclusion_quantitative`
1.00/1.00, `inclusion_quantitative` 0.80 precision) and it captures the
categorical exclusions the larger 14B model oddly dropped (0.62/0.71 vs. 0.12/0.14)
— exactly what the deterministic Critic leans on. On the **real set** overall
recall is **0.37 — 2.8× the 8B baseline's 0.13**: on long, messy
ClinicalTrials.gov text it recovers far more of the criteria that drive screening.
`qwen2.5:14b` edges it there (0.47) but needs ~10.7 GiB to load, so 7B captures
most of the real-world gain within the memory budget — the reason it is the
default. Two caveats: (1) curated gold counts are tiny (1–2 per some types), so a
single miss swings a row hard — `unparseable` 1.00 → 0.00 is one missed item, not
a trend; (2) `unparseable` recall is 0.00 on **both** sets — the model tends to
extract borderline criteria as concrete values rather than routing genuinely
vague ones to `unparseable`, so the deterministic Critic's vague-language rules
(which key off the protocol text, not the `unparseable` list) remain the real
backstop, and the LLM semantic pass (`run_llm_semantic_review`) catches
contradictions on top. Numbers move with the model; treat them as a snapshot, not
a contract. Reproduce with `make eval` (prints curated, real, and combined tables).

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
      screening.py             # Screening use-cases (create/stream/approve/edit/state)
      criteria_edits.py        # Before/after diff of a reviewer's criteria revision
      comparison.py            # Two runs paired side by side: criteria + cohort verdicts
      cohort.py                # Which bucket a patient lands in — one rule, every reader
      attrition.py             # Per-criterion cohort attrition: what screened patients out
      timeline.py              # The event log as an audit trail: retries, gate, attribution
      provenance.py            # criterion source_text → character span in the protocol
      report.py                # Self-contained, printable HTML screening report
      rules.py                 # The compliance rules database, rendered for reading
      metrics.py               # Custom Prometheus metric definitions (one home)
      metrics_summary.py       # Those metrics reduced for the in-app dashboard
      notifications.py         # Gate/escalation notifications (webhook + email, PHI-free)
      sse.py                   # Server-Sent Events wire format (one place)
      llm.py, pdf.py           # LLM factory, PDF eligibility-section extraction
  tests/
frontend/
  next.config.ts               # Static export, /api dev proxy
  src/
    app/                       # App Router: layout shell + `/` (new screening)
    hooks/useScreenerStream.ts # SSE consumption of graph events
    lib/sse.ts                 # Framing for SSE bodies fetch returns (POST/PATCH)
    components/                # ScreeningRun, AgentCard, CriteriaTable, matches
    components/review/         # Review queue, criteria editor, before/after diff
    components/runs/           # Runs index, one run replayed, two runs compared
    components/provenance/     # Criteria beside the protocol, with source highlighting
    components/rules/          # The compliance rules viewer findings link into
    components/metrics/        # In-app funnel, rejection breakdown, loop depth
    components/batch/          # Batch upload progress, one row per protocol
    lib/batch.ts               # The batch's stream queue and phase mapping
    components/report-download.tsx # Export a run as a self-contained HTML report
    types.ts                   # Shared API contract, mirrors the Pydantic schemas
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

### TrialGate v2 — product & experience layer

With the production-hardening pillars above complete, the next initiative is the
**product & experience layer**, tracked under the
[TrialGate v2 epic (#46)](../../issues/46): a rename to **TrialGate**, a
**Next.js** frontend migration, **authentication**, and the reviewer-facing
features that make the pipeline demoable end-to-end.

| Pillar | Issues | What it delivers |
|---|---|---|
| **A · Frontend platform** | [#47](../../issues/47) `P1` · [#48](../../issues/48) `P1` · [#49](../../issues/49) `P2` | Next.js + TypeScript migration, dashboard shell (sidebar nav, Tailwind, shadcn/ui), skeletons + Framer Motion |
| **B · Auth & access** | [#50](../../issues/50) `P1` | Authentication + reviewer/admin roles + login; gates the patient-matching step |
| **C · Core features** | [#51](../../issues/51) `P1` · [#52](../../issues/52) `P1` · [#53](../../issues/53) `P1` | Past-runs history, human-readable Critic/Matcher output, human-in-the-loop edit-and-rerun |
| **D · Audit & reporting** | [#54](../../issues/54) `P2` · [#55](../../issues/55) `P2` · [#56](../../issues/56) `P2` · [#57](../../issues/57) `P3` · [#58](../../issues/58) `P3` · [#59](../../issues/59) `P3` | Provenance highlighting, event timeline, downloadable report, rules viewer, in-app metrics, run comparison |
| **E · Platform extras** | [#60](../../issues/60) `P3` · [#61](../../issues/61) `P3` | Notify-on-gate/escalation, batch upload |
| **F · Rebrand** | [#62](../../issues/62) `P1` | Rename the product to TrialGate across docs & code (repo slug unchanged) |

Suggested order: **F1 → A1 → A2 → B1** (foundation), then **C1 → C2 → C3**
(core), then D1–D3 + A3 (depth), then the remaining P3 items.

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
`next build`) jobs. A [`pr-title.yml`](.github/workflows/pr-title.yml) check enforces a
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

A production deployment would instead run the published backend and
nginx-frontend images as separate containers with a durable Postgres checkpointer
(`CHECKPOINT_BACKEND=postgres`) and a real LLM backend, fronted by a
[`/ready`](#health--readiness)-gated rolling update — but that path isn't wired
into this repo's CD.

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
| `OLLAMA_MODEL` | `qwen2.5:7b` | Local model tag (~4.7GB, ~6GiB to load; fits 16GB RAM and Docker's default VM) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server address |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Hosted model id |
| `ANTHROPIC_API_KEY` | — | **Required** when `LLM_PROVIDER=anthropic` |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature (0–1) |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `AUTH_ENABLED` | `true` | Gate `/api` behind a session; `false` only for single-user local runs / load tests |
| `AUTH_SECRET` | — | Session signing key. **Required in production** — unset means a random per-process key (sessions die on restart, replicas reject each other) |
| `AUTH_USERS` | — | Accounts as `email:role:hash` entries; mint a hash with `python -m app.auth hash`. **Replaces** the demo accounts |
| `AUTH_DEMO_USERS` | `true` | Seed the published demo accounts when `AUTH_USERS` is empty; set `false` in production |
| `AUTH_SESSION_TTL_SECONDS` | `28800` | Session lifetime (8h) |
| `AUTH_COOKIE_SECURE` | `false` | Add the cookie's `Secure` attribute; set `true` behind TLS |
| `RATE_LIMIT_LOGIN` | `10/minute` | Per-IP cap on login attempts |
| `NOTIFY_ENABLED` | `false` | Notify reviewers when a run parks at the gate or escalates; requires a channel below |
| `NOTIFY_WEBHOOK_URL` | — | Slack-compatible incoming webhook (or any JSON POST endpoint) |
| `NOTIFY_EMAIL_TO` / `NOTIFY_EMAIL_FROM` / `NOTIFY_SMTP_HOST` | — | Email channel; all three required together |
| `NOTIFY_SMTP_PORT` / `NOTIFY_SMTP_USERNAME` / `NOTIFY_SMTP_PASSWORD` / `NOTIFY_SMTP_STARTTLS` | `587` / — / — / `true` | SMTP submission settings |
| `NOTIFY_TIMEOUT_SECONDS` | `5` | Per-channel ceiling; also the most a notification can add to a run |
| `NOTIFY_BASE_URL` | — | Public frontend URL, used to link to the run; unset omits the link |
| `MAX_PARSE_ATTEMPTS` | `3` | Parser retries before human escalation (1–10) |
| `RULES_PATH` | `app/rules/compliance_rules.yaml` | Compliance rules database |
| `PATIENTS_PATH` | `app/data/patients.json` | Synthetic EHR location |
| `CHECKPOINT_BACKEND` | `sqlite` | `memory` (tests), `sqlite` (durable single-node), `postgres` (multi-replica) |
| `SQLITE_PATH` | `screenings.sqlite` | sqlite file shared by the checkpointer and screening store |
| `POSTGRES_DSN` | — | **Required** when `CHECKPOINT_BACKEND=postgres`; install with `pip install -e ".[postgres]"` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `console` | `console` (human-readable) or `json` (one object per line) |
