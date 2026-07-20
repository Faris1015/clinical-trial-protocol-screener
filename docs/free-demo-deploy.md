# Free demo deployment (no credit card)

A one-container, zero-cost public demo of the screener: the FastAPI backend
serves **both** the React SPA and the API from a single origin, in
`LLM_PROVIDER=stub` mode (deterministic, no GPU, no API key). No CORS, no second
host, no credit card.

This is a **demo-only topology**, separate from a real production deployment
(which would run the backend and an nginx frontend as two containers with a real
LLM and a durable Postgres checkpointer).

## What makes it free

- **Stub LLM** ([`app/services/stub_llm.py`](../backend/app/services/stub_llm.py)) —
  canned, schema-valid extractions instead of model calls. The whole pipeline runs
  (routing → parse → critique → HITL gate → match), but the analysis is fixed, so
  there's no inference cost. Swap to a real model later by setting
  `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` (that part is not free).
- **One image** ([`deploy/demo/Dockerfile`](../deploy/demo/Dockerfile)) — builds
  the SPA and copies it into the backend image; FastAPI serves it via
  `FRONTEND_DIST` (see [`app/main.py`](../backend/app/main.py)).
- **Ephemeral SQLite** — no managed database. Screenings reset when the instance
  restarts; the synthetic EHR is reseeded on boot. Fine for a demo.

## Try it locally first

```bash
docker build -f deploy/demo/Dockerfile -t screener-demo .
docker run --rm -p 8000:8000 screener-demo
# open http://localhost:8000  — upload a protocol, approve, see matches
```

## Option A — Render (simplest, auto-deploys from GitHub)

Builds `deploy/demo/Dockerfile` straight from the repo via the checked-in
[`render.yaml`](../render.yaml) blueprint. Free plan, no card.

1. Push this branch to GitHub (already done if you're reading this in a PR).
2. Go to <https://dashboard.render.com> → **New → Blueprint** → connect the repo.
3. Render reads `render.yaml`, creates the `protocol-screener-demo` web service on
   the **Free** plan, and deploys. First build ~3–5 min.
4. Open the assigned `https://protocol-screener-demo.onrender.com` URL.

Caveats: the free service **sleeps after ~15 min idle** (first hit cold-starts
~50 s) and has 512 MB RAM. `autoDeploy` pushes every commit to `main`.

## Option B — Hugging Face Space (stays warm longer)

A Space is its own git repo; [`deploy/huggingface/`](../deploy/huggingface/)
holds the two files it needs. The Dockerfile clones this repo at build time.

1. Create a Space at <https://huggingface.co/new-space> → SDK: **Docker** →
   **Blank**. Free, no card.
2. Into the Space repo, copy **both** files from `deploy/huggingface/`:
   `README.md` (has the required Space metadata + `app_port: 8000`) and
   `Dockerfile`. Commit/push to the Space.
3. The Space builds and starts. Open `https://<you>-<space-name>.hf.space`.

Notes:

- Sleeps only after ~48 h idle — better for a link you'll share.
- To build from an **un-merged branch**, add a build arg in the Space's
  **Settings → Variables and secrets**: `REPO_REF=<branch-name>` (defaults to
  `main`). Point it back to `main` once this is merged.

## Recommended pick

- Sharing a portfolio link people click occasionally → **Hugging Face** (warmer).
- Want it wired to auto-deploy on every push with the least clicks → **Render**.

## Turning it into a real (non-stub) demo later

Set on the host (Render env vars, or HF Space secrets):

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

That bills per token, so keep the create rate limit on (it is, by default). For
durable screenings across restarts, also point `CHECKPOINT_BACKEND=postgres` +
`POSTGRES_DSN=...` at a free Neon/Supabase database.