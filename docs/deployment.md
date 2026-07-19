# Deployment (CD)

Merge to `main` → images built and pushed to GHCR → zero-downtime rolling deploy
on [Fly.io](https://fly.io) → post-deploy smoke test. Once the one-time setup
below is done, shipping is just merging a PR.

- **Pipeline:** [`.github/workflows/cd.yml`](../.github/workflows/cd.yml)
- **Live demo:** https://screener-frontend.fly.dev

## Topology

Four Fly apps in one org, wired over Fly's private network:

| App | Public? | Role |
|---|---|---|
| `screener-frontend` | **yes** — the demo URL | nginx: serves the SPA, proxies `/api`, `/health`, `/ready` to the backend |
| `screener-backend` | no (flycast only) | FastAPI + LangGraph API; stateless (state in Postgres) → runs 2 machines |
| `screener-ollama` | no (flycast only) | dedicated LLM inference; model cached on a volume |
| `screener-db` | no | Fly Postgres — LangGraph checkpointer + screening store |

The frontend is the only app with a public IP. It reaches the backend at
`screener-backend.flycast`; the backend reaches inference at
`screener-ollama.flycast:11434` and state at the attached Postgres.

## How zero-downtime works

The backend is **stateless** — all durable state lives in Fly Postgres
(`CHECKPOINT_BACKEND=postgres`), and patient data is regenerated deterministically
on each boot — so it runs **two machines**. `flyctl deploy` uses a **rolling**
strategy: it replaces one machine at a time and will not drain the old machine
until the new one returns `200` from the [`/ready`](../.github/workflows/cd.yml)
health check (LLM + rules + EHR + DB all reachable). If a new machine never
passes — e.g. it's killed mid-rollout — the release fails and the old machines
keep serving. A client polling the public URL sees no downtime.

> Both `screener-frontend` and `screener-backend` set `min_machines_running = 2`.
> Drop either to a single machine and its rollout becomes an in-place swap with a
> brief gap — the two-machine count is what the zero-downtime guarantee rests on.

## One-time setup

Requires the [`flyctl` CLI](https://fly.io/docs/flyctl/install/) and a Fly account.

1. **Create the apps** (names must match the configs):
   ```bash
   flyctl apps create screener-backend
   flyctl apps create screener-frontend
   flyctl apps create screener-ollama
   ```

2. **Postgres** — create and attach to the backend, exposing the DSN under the
   env var the app reads (`POSTGRES_DSN`, not Fly's default `DATABASE_URL`):
   ```bash
   flyctl postgres create --name screener-db --region iad
   flyctl postgres attach screener-db --app screener-backend --variable-name POSTGRES_DSN
   ```

3. **Ollama** — deploy it (builds the wrapper image that pulls the model on first
   boot), give it a private address, and create the model volume:
   ```bash
   cd deploy/ollama
   flyctl volumes create ollama_models --app screener-ollama --region iad --size 20
   flyctl ips allocate-v6 --private --app screener-ollama
   flyctl deploy                       # uses ./fly.toml + ./Dockerfile
   cd -
   ```
   > `llama3.1:8b` needs a memory-heavy VM (see `[[vm]]` in
   > [`deploy/ollama/fly.toml`](../deploy/ollama/fly.toml)) and is slow on CPU.
   > For a cheaper/faster demo, set a smaller `OLLAMA_MODEL` (e.g. `llama3.2:3b`)
   > in **both** `deploy/ollama/fly.toml` and `deploy/fly/backend.toml`.

4. **Backend private address** (no public IP — reached only via the frontend):
   ```bash
   flyctl ips allocate-v6 --private --app screener-backend
   ```

5. **GHCR image visibility** — the repo is public; make the two pushed packages
   (`…-backend`, `…-frontend`) **public** too (GitHub → repo → Packages → each
   package → Package settings → Change visibility) so Fly can pull them without
   registry credentials. The first CD run creates the packages; flip them once.

6. **GitHub `production` Environment** (repo → Settings → Environments →
   `production`):
   - Secret **`FLY_API_TOKEN`** — `flyctl tokens create deploy` (scoped to these apps).
   - *(optional)* Variable **`PRODUCTION_URL`** if the frontend URL differs from
     the default `https://screener-frontend.fly.dev`.
   - *(optional)* Secret **`SLACK_WEBHOOK_URL`** for a smoke-test-failure ping.
   - *(optional)* A **required-reviewer** protection rule → every merge becomes a
     gated manual promotion.

After this, a merge to `main` deploys automatically.

## Rollback

Every build is tagged with its commit SHA in GHCR, so rolling back is
re-deploying a previous tag — no rebuild:

```bash
# Roll the backend back to a known-good commit (short SHA, as tagged):
flyctl deploy --config deploy/fly/backend.toml \
  --image ghcr.io/faris1015/clinical-trial-protocol-screener-backend:<sha>

# ...and the frontend to the same commit:
flyctl deploy --config deploy/fly/frontend.toml \
  --image ghcr.io/faris1015/clinical-trial-protocol-screener-frontend:<sha>
```

The rollback is itself a `/ready`-gated rolling deploy, so it's zero-downtime
too. `flyctl releases --app screener-backend` lists recent releases and their
image tags if you need to find the last good SHA.

## Smoke test

[`scripts/smoke_test.sh`](../scripts/smoke_test.sh) runs in CD after the deploy
and is self-contained — point it at any environment by hand:

```bash
scripts/smoke_test.sh https://screener-frontend.fly.dev <expected-sha>
```

It checks `/health` (asserting the reported `commit` matches the deployed SHA,
proving the rollout cut over), `/ready` (all dependencies up), then creates a
stub screening and drives the pipeline through the stream to a healthy terminal
state. Any failure exits non-zero and fails the workflow.