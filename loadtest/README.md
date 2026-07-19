# Load testing (#10)

Locust-based load test of the full reviewer journey: **upload → hold the SSE
stream to the human-in-the-loop gate → approve → fetch results**.

The measured baselines, the bottleneck this test found and fixed, and the known
single-instance limits live in [`docs/performance.md`](../docs/performance.md).

## Quick start

Run the app in **stub-LLM mode** so the numbers reflect the app's own overhead
(routing, SSE fan-out, the concurrency gate, the checkpointer) rather than model
inference. Two ways:

**A. Docker (closest to production):**

```bash
docker compose -f docker-compose.loadtest.yml up --build     # backend on :8000
make loadtest                                                 # 50-user, 5-min run
```

**B. Local uvicorn (fastest to iterate):**

```bash
pip install -e "backend/.[loadtest]"       # once: installs Locust

# from backend/, start the server in stub mode with the gate opened wide:
LLM_PROVIDER=stub RATE_LIMIT_ENABLED=false MAX_CONCURRENT_SCREENINGS=64 \
  .venv/bin/uvicorn app.main:app --port 8000

# in another shell, from the repo root:
make loadtest
```

## Knobs

`make loadtest` accepts overrides:

```bash
make loadtest USERS=200 SPAWN_RATE=100 RUN_TIME=5m
make loadtest LOADTEST_HOST=http://localhost:8000
```

Interactive web UI (http://localhost:8089) for exploring:

```bash
make loadtest-ui
```

Server-side knobs worth varying:

| Env var                     | Effect                                                            |
|-----------------------------|------------------------------------------------------------------|
| `STUB_LATENCY_SECONDS`      | Model a slow LLM backend (per graph call) to stress the gate/pool |
| `MAX_CONCURRENT_SCREENINGS` | Lower it to watch the 429 backpressure gate trip on purpose       |
| `RATE_LIMIT_ENABLED`        | Keep `false` for load tests, or `true` to measure limiter effects |

## Reading the results

- **Backpressure is not failure.** When the concurrency gate is saturated the
  server returns `429 + Retry-After`. The locustfile reports those under the
  request's normal name and does **not** count them as failures, so the failure
  rate reflects real defects (5xx, timeouts, malformed responses) only.
- Each Locust "user" runs one full journey per iteration with a short think-time,
  so the load is closed-loop rather than a synthetic hammer.