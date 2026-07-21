"""FastAPI app: HTTP edge for the screener — routing, error contract, logging.

Routes are thin translators: they read the request, resolve the wired
dependencies (store, graph), and hand off to `app.services.screening`, which
owns all screening business logic. Nothing here builds state, invokes the
graph, or formats SSE frames directly.

Error contract: domain exceptions (app/exceptions.py) map to status codes in
one handler — clients get a JSON body, never a stack trace. The SSE stream
terminates with `__error__` instead of dying silently when a node blows up.
"""

import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.graph.state import CompiledStateGraph
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.exceptions import PayloadTooLargeError, ScreenerError
from app.health import app_version, readiness
from app.logging_config import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.persistence import Persistence, ScreeningStore, open_persistence
from app.services import screening, sse
from app.services.concurrency import ConcurrencyLimiter, release_after
from app.services.uploads import read_upload_capped, validate_content_type

# Probes fire every few seconds from orchestrators/load balancers; keep them out
# of the INFO access log so they don't drown the request stream. /metrics is
# scraped by Prometheus on the same cadence, so it belongs here too.
_QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})

# A client-supplied X-Request-ID is echoed into every log line for the request
# and reflected in the response header. Constrain it to a short, safe charset so
# it can't be used to forge/inject log lines (console format) or bloat logs; an
# out-of-spec value is dropped in favor of a freshly minted UUID.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Resolve settings at import time so a misconfigured deployment fails at
# startup (e.g. LLM_PROVIDER=anthropic without ANTHROPIC_API_KEY), not
# mid-screening. configure_logging() re-applies settings-driven config (it also
# runs on logging_config import, so module-level loggers are already wired).
settings = get_settings()
configure_logging()
log = get_logger("api")

# IP-keyed rate limiter (#15). Limits are read from settings *per request* via
# callables, so a test can tighten them without re-importing the module. Disabled
# wholesale via RATE_LIMIT_ENABLED so the test suite isn't throttled by this
# process-wide in-memory counter.
limiter = Limiter(key_func=get_remote_address, enabled=settings.rate_limit_enabled)

# Bounds concurrent in-flight graph runs on this instance; saturation → 429.
active_screenings = ConcurrencyLimiter(
    settings.max_concurrent_screenings, settings.concurrency_retry_after_seconds
)

# Durable state lives here, wired up in the lifespan. No module-level mutable
# dicts: a restart, crash, or deploy rehydrates everything from the store, and
# a second worker sees the same rows (see app/persistence.py).
_persistence: Persistence | None = None
graph: CompiledStateGraph | None = None


def _store() -> ScreeningStore:
    assert _persistence is not None, "persistence not initialized — is the app started?"
    return _persistence.store


def _graph() -> CompiledStateGraph:
    assert graph is not None, "graph not initialized — is the app started?"
    return graph


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _persistence, graph
    _persistence = await open_persistence(settings)
    graph = screening.build_screening_graph(_persistence.checkpointer)
    log.info("app.started", checkpoint_backend=_persistence.backend)
    try:
        yield
    finally:
        await _persistence.aclose()
        _persistence = None
        graph = None


app = FastAPI(title="Clinical Trial Protocol Screener", lifespan=lifespan)
# slowapi reads the limiter off app.state and its handler turns a tripped limit
# into a 429 (with Retry-After) that our error contract shape wraps below.
app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Standard HTTP metrics (request count, latency histogram, in-flight) at
# GET /metrics (#7). Custom domain metrics live in app/services/metrics.py and
# register on the same default registry, so one scrape returns both. Excluded
# from the OpenAPI schema and the access log (see _QUIET_PATHS) — it's an
# operator endpoint, not part of the API contract.
if settings.metrics_enabled:
    Instrumentator(excluded_handlers=["/metrics", "/health", "/ready"]).instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False
    )


@app.middleware("http")
async def correlation_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind a per-request `request_id` into every log line and echo it back.

    A client-supplied `X-Request-ID` is honored (so a trace spans services) when
    it matches `_REQUEST_ID_RE`; otherwise one is minted. `thread_id` is bound
    later, inside the handlers that know it, and rides the same contextvars into
    the graph nodes.
    """
    incoming = request.headers.get("x-request-id")
    request_id = incoming if incoming and _REQUEST_ID_RE.match(incoming) else str(uuid4())
    clear_contextvars()
    bind_contextvars(request_id=request_id)
    quiet = request.url.path in _QUIET_PATHS
    started = time.perf_counter()
    if not quiet:
        log.info("request.start", method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
        if not quiet:
            log.info(
                "request.finish",
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        log.error(
            "request.error",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            exc_info=True,
        )
        raise
    finally:
        clear_contextvars()


@app.exception_handler(ScreenerError)
async def screener_error_handler(request: Request, exc: ScreenerError) -> JSONResponse:
    log.warning(
        "screener_error",
        error=type(exc).__name__,
        status_code=exc.http_status,
        detail=str(exc),
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": type(exc).__name__, "detail": str(exc)},
        headers=exc.headers or None,
    )


def _retry_after_seconds(request: Request) -> int | None:
    """Seconds until the tripped limit's window resets, from slowapi's storage.

    `headers_enabled` is left off (it would force a `response` param onto every
    endpoint), so we derive Retry-After here from the same window stats slowapi's
    own header injector uses.
    """
    current = getattr(request.state, "view_rate_limit", None)
    if not current:
        return None
    try:
        reset_at, _remaining = limiter.limiter.get_window_stats(current[0], *current[1])
        return max(1, int(reset_at - time.time()))
    except Exception:  # noqa: BLE001 - Retry-After is best-effort, never fatal
        return None


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Tripped rate limit → 429 in our error-contract shape, with a Retry-After
    so clients can back off."""
    log.warning("rate_limited", path=request.url.path)
    retry_after = _retry_after_seconds(request)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=429,
        content={
            "error": "RateLimitExceeded",
            "detail": "Rate limit exceeded; slow down and retry after the window resets.",
        },
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Same body shape as domain errors, so clients parse one error contract.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "HTTPError", "detail": exc.detail},
    )


@app.get("/health")
async def health() -> dict:
    """Liveness probe: the process is up and serving requests.

    Deliberately dependency-free so the container HEALTHCHECK reflects only
    "is the server alive" — a hung or crashed process, not a blipping
    dependency. Dependency readiness lives in /ready.
    """
    return {"status": "ok", **app_version()}


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe: 200 only when every dependency the request path needs
    is reachable; 503 with a per-check breakdown otherwise.

    Checks (LLM, rules, patients, store) run concurrently under a per-check
    timeout, so a single hung dependency can't blow the probe's time budget.
    """
    all_ok, checks = await readiness(_store())
    body = {"status": "ok" if all_ok else "degraded", "checks": checks, **app_version()}
    return JSONResponse(status_code=200 if all_ok else 503, content=body)


@app.post("/api/screenings")
@limiter.limit(lambda: settings.rate_limit_create)
async def create_screening(request: Request, file: UploadFile) -> dict:
    # Reject an oversized upload from its declared size before touching the body,
    # so a 100 MB spam POST is turned away in well under a second (the streamed
    # read below is the exact guard for a spoofed/absent Content-Length).
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + 8192:
        raise PayloadTooLargeError(
            f"Upload exceeds the {settings.max_upload_bytes} byte limit.",
            headers={"Connection": "close"},
        )
    validate_content_type(file.content_type, file.filename, settings.upload_content_type_set)
    raw = await read_upload_capped(file, settings.max_upload_bytes)
    thread_id = await screening.create_screening(
        _store(),
        file.filename,
        raw,
        content_type=file.content_type,
        max_pdf_pages=settings.max_pdf_pages,
        max_text_chars=settings.max_protocol_text_chars,
    )
    return {"thread_id": thread_id}


@app.get("/api/screenings")
@limiter.limit(lambda: settings.rate_limit_read)
async def list_screenings(request: Request) -> list[dict]:
    return await screening.list_screenings(_store())


@app.get("/api/screenings/{thread_id}/stream")
@limiter.limit(lambda: settings.rate_limit_read)
async def stream_screening(request: Request, thread_id: str) -> StreamingResponse:
    # Fail fast (429 + Retry-After) *before* the response commits when every
    # concurrency slot is taken; the slot is held for the stream's lifetime and
    # freed in release_after's finally, even if the client disconnects.
    active_screenings.acquire()
    try:
        frames = await screening.stream_screening(_store(), _graph(), thread_id)
    except BaseException:
        active_screenings.release()
        raise
    guarded = release_after(frames, active_screenings)
    heartbeated = sse.with_heartbeats(
        guarded,
        heartbeat_seconds=settings.sse_heartbeat_seconds,
        idle_timeout_seconds=settings.sse_idle_timeout_seconds,
    )
    return StreamingResponse(heartbeated, media_type="text/event-stream")


@app.post("/api/screenings/{thread_id}/approve")
@limiter.limit(lambda: settings.rate_limit_create)
async def approve_screening(request: Request, thread_id: str) -> StreamingResponse:
    # Mirror the stream route: the matcher can run for minutes on a local model,
    # so hold a concurrency slot for its lifetime and stream its progress rather
    # than blocking the POST until the whole cohort is scored (which times out
    # the client and provokes duplicate approve clicks). Eager validation inside
    # approve_screening raises before the response commits, so the slot acquired
    # here is released on that path too.
    active_screenings.acquire()
    try:
        frames = await screening.approve_screening(_store(), _graph(), thread_id)
    except BaseException:
        active_screenings.release()
        raise
    guarded = release_after(frames, active_screenings)
    heartbeated = sse.with_heartbeats(
        guarded,
        heartbeat_seconds=settings.sse_heartbeat_seconds,
        # The matcher emits progress between calls (resetting this clock), but a
        # single slow cohort-mapping call needs a longer window than the pre-
        # approval phase.
        idle_timeout_seconds=settings.sse_matcher_idle_timeout_seconds,
    )
    return StreamingResponse(heartbeated, media_type="text/event-stream")


@app.get("/api/screenings/{thread_id}/state")
@limiter.limit(lambda: settings.rate_limit_read)
async def get_state(request: Request, thread_id: str) -> dict:
    return await screening.get_screening_state(_store(), _graph(), thread_id)


def mount_frontend(app: FastAPI, dist: Path | None) -> bool:
    """Single-service demo mode: serve a built SPA bundle from this same app.

    When `dist` points at a directory containing index.html, mount it at "/" so
    one container hosts the whole demo (SPA + API, same origin, no CORS). Must be
    called AFTER every API/operator route is registered: the catch-all mount is
    matched last, so those routes always win; StaticFiles(html=True) then serves
    index.html and the hashed assets, all this single-page app needs. Returns
    whether it mounted. A no-op in the split production topology (dist unset —
    nginx serves the SPA there). See deploy/demo/Dockerfile, docs/free-demo-deploy.md.
    """
    if not (dist and (dist / "index.html").is_file()):
        return False
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    log.info("frontend.mounted", path=str(dist))
    return True


mount_frontend(app, settings.frontend_dist)
