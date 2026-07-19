# Performance & load testing (#10)

How the screener behaves under concurrent load, the bottleneck the load test
found and fixed, and the known limits of a single instance.

The harness is [`loadtest/locustfile.py`](../loadtest/locustfile.py); how to run
it is in [`loadtest/README.md`](../loadtest/README.md). Everything below is
reproducible with `make loadtest`.

## TL;DR

- A single instance in **stub-LLM mode** sustains **50 concurrent full
  screening journeys at p95 ≈ 12–21 ms with a < 0.5 % error rate**, and holds a
  **5-minute soak with zero memory growth** (RSS flat at 156 MB).
- The load test found a real bug: under concurrency the SQLite screening store
  **fast-failed writes with "database is locked"** — **~97 % of uploads failed
  at 50 users**. Root cause: the store connection used Python's default implicit
  transactions, whose write-lock acquisition takes an *immediate* `SQLITE_BUSY`
  that `busy_timeout` can't absorb. Switching the connection to **autocommit**
  dropped the same load to **0.4 %**. Fix + regression test:
  [`app/persistence.py`](../backend/app/persistence.py),
  `test_sqlite_store_connection_is_autocommit`.
- Above ~50 concurrent screenings the single-node SQLite write path is the
  ceiling (error rate climbs to ~2.6 % at 200 users). The scale-out answer is
  already wired: `CHECKPOINT_BACKEND=postgres`.

## Methodology

Each simulated user runs the **real reviewer journey** end to end, then repeats
after a short think-time (0.5–2 s):

1. `POST /api/screenings` — upload a protocol → `thread_id`
2. `GET .../{id}/stream` — hold the SSE stream until the graph interrupts at the
   human-in-the-loop gate
3. `POST .../{id}/approve` — approve past the gate → matched patients
4. `GET .../{id}/state` — fetch the final results

Runs use **`LLM_PROVIDER=stub`** ([`app/services/stub_llm.py`](../backend/app/services/stub_llm.py)),
a zero-inference in-process model returning canned, schema-valid extractions.
That deliberately removes model latency so the numbers isolate the app's own
overhead — routing, SSE fan-out, the concurrency gate, and the checkpointer —
which is what a capacity baseline should measure. `STUB_LATENCY_SECONDS`
reintroduces synthetic per-call latency when you want to study how inference
time interacts with the threadpool and the concurrency gate.

**Backpressure ≠ failure.** When the concurrency gate is saturated the server
returns `429 + Retry-After`; the harness records those without counting them as
failures, so the error rate reflects only real defects (5xx, timeouts,
malformed bodies).

**Test environment** (indicative dev workstation — reproduce locally for your
own hardware): Apple Silicon, 14 cores, Python 3.14, one uvicorn worker, SQLite
backend, `MAX_CONCURRENT_SCREENINGS=64`, rate limiting off.

## Baseline results

Scenario sweep, 60 s per level, all users spawned at once, stub latency 0:

| Concurrent users | Requests | Error rate | p50 | p95 | p99 | Throughput | Peak RSS |
|-----------------:|---------:|-----------:|----:|----:|----:|-----------:|---------:|
| 10               |    1,872 |     0.00 % | 5 ms  | 9 ms   | 11 ms  | 51 req/s  | 137 MB |
| 50               |    9,296 |     0.31 % | 3 ms  | 21 ms  | 43 ms  | 120 req/s | 148 MB |
| 200              |   25,834 |     2.60 % | 11 ms | 160 ms | 200 ms | 270 req/s | 160 MB |

Latencies are per-request across all four journey steps aggregated; `approve`
(which runs the Matcher synchronously) is the slowest step and dominates the
tail at 200 users.

### Soak (memory)

50 concurrent users, 5 minutes:

| Metric        | Result                                   |
|---------------|------------------------------------------|
| Requests      | 46,992                                   |
| Error rate    | 0.20 %                                   |
| Latency       | p50 3 ms · p95 12 ms · p99 15 ms         |
| Throughput    | 85 req/s                                  |
| **Memory**    | **RSS flat at 156 MB start→end — no growth** |

RSS was sampled every 18 s for the whole run and never moved off 156.1 MB, so
per-screening state (checkpoints, SSE generators, concurrency slots) is released
cleanly and does not accumulate.

## Bottleneck found & fixed: SQLite store write-lock fast-fail

The first 50-user run failed catastrophically — not slow, but **erroring**:

| Metric (50 users, 45 s) | Before (implicit txns) | After (autocommit) |
|-------------------------|-----------------------:|-------------------:|
| Upload (`create`) failures | **1,679 / 1,733 (96.9 %)** | **7 / 1,747 (0.40 %)** |
| Completed-journey requests | 1,895                  | 6,967 (3.7×)       |
| `database is locked` errors | 1,724                 | 14                 |

Every failure was `sqlite3.OperationalError: database is locked` on the store's
`INSERT`/`UPDATE`, and — the tell — it failed in **~7 ms**, not after the 5 s
`busy_timeout`, so the writer wasn't *waiting* for the lock at all.

**Root cause.** The screening store and the LangGraph checkpointer hold separate
connections to the same SQLite file. Under load the checkpointer writes on
nearly every graph step, so the WAL write lock is contended constantly. The
store connection used Python's **default implicit transaction management**,
under which a contended write is taken via a path that returns `SQLITE_BUSY`
*immediately* to avoid deadlock — a busy state `busy_timeout` does not cover.
So instead of briefly waiting out the checkpointer, store writes bounced
instantly as 500s.

**Fix.** Open the store connection in **autocommit mode**
(`aiosqlite.connect(path, isolation_level=None)`). Each `INSERT`/`UPDATE` is then
a standalone statement that acquires the write lock directly — the path where
`busy_timeout` *is* honored — so a contended writer waits out the brief lock
instead of erroring. The store only ever issues single-statement writes, so it
needs no multi-statement transactions and loses nothing. One line, with the
reasoning captured in [`app/persistence.py`](../backend/app/persistence.py) and
guarded by `test_sqlite_store_connection_is_autocommit`.

## Known limits & guidance

- **Single-node SQLite write ceiling (~50 concurrent screenings).** Even after
  the fix, a residual < 0.5 % of writes can still hit an unabsorbable
  `SQLITE_BUSY` under heavy contention, and the error rate rises to ~2.6 % at
  200 concurrent users. One SQLite file admits one writer at a time; that is the
  hard limit of the single-node backend.
  - **Scale-out:** `CHECKPOINT_BACKEND=postgres` (already implemented) moves both
    the checkpointer and the store onto Postgres for multi-replica deployments.
  - **Stay-single-node:** keep `MAX_CONCURRENT_SCREENINGS` at its default (4) so
    the gate returns `429 + Retry-After` well before write contention degrades
    into errors. The wide gate (64) used for these tests is a benchmarking
    setting, not a production one.
- **`approve` is synchronous and CPU-bound.** It runs the Matcher over the full
  patient cohort inline, so it dominates the latency tail under high concurrency.
  It is bounded by the same concurrency gate as streaming.
- **Real inference dominates in production.** With a real LLM, per-screening
  latency is set by the model, not the app. Re-run with `STUB_LATENCY_SECONDS`
  (or briefly against real Ollama) to size a deployment for a given backend.