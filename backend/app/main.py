"""FastAPI app: HTTP edge for the screener — routing, error contract, logging.

Routes are thin translators: they read the request, resolve the wired
dependencies (store, graph), and hand off to `app.services.screening`, which
owns all screening business logic. Nothing here builds state, invokes the
graph, or formats SSE frames directly.

Error contract: domain exceptions (app/exceptions.py) map to status codes in
one handler — clients get a JSON body, never a stack trace. The SSE stream
terminates with `__error__` instead of dying silently when a node blows up.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langgraph.graph.state import CompiledStateGraph
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.exceptions import ScreenerError
from app.health import app_version, readiness
from app.logging_config import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.persistence import Persistence, ScreeningStore, open_persistence
from app.services import screening

# Probes fire every few seconds from orchestrators/load balancers; keep them out
# of the INFO access log so they don't drown the request stream.
_QUIET_PATHS = frozenset({"/health", "/ready"})

# Resolve settings at import time so a misconfigured deployment fails at
# startup (e.g. LLM_PROVIDER=anthropic without ANTHROPIC_API_KEY), not
# mid-screening. configure_logging() re-applies settings-driven config (it also
# runs on logging_config import, so module-level loggers are already wired).
settings = get_settings()
configure_logging()
log = get_logger("api")

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind a per-request `request_id` into every log line and echo it back.

    A client-supplied `X-Request-ID` is honored (so a trace spans services);
    otherwise one is minted. `thread_id` is bound later, inside the handlers
    that know it, and rides the same contextvars into the graph nodes.
    """
    request_id = request.headers.get("x-request-id") or str(uuid4())
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
async def create_screening(file: UploadFile) -> dict:
    thread_id = await screening.create_screening(_store(), file.filename, await file.read())
    return {"thread_id": thread_id}


@app.get("/api/screenings")
async def list_screenings() -> list[dict]:
    return await screening.list_screenings(_store())


@app.get("/api/screenings/{thread_id}/stream")
async def stream_screening(thread_id: str) -> StreamingResponse:
    frames = await screening.stream_screening(_store(), _graph(), thread_id)
    return StreamingResponse(frames, media_type="text/event-stream")


@app.post("/api/screenings/{thread_id}/approve")
async def approve_screening(thread_id: str) -> dict:
    return await screening.approve_screening(_store(), _graph(), thread_id)


@app.get("/api/screenings/{thread_id}/state")
async def get_state(thread_id: str) -> dict:
    return await screening.get_screening_state(_store(), _graph(), thread_id)
