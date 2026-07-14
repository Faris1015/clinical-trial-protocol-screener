"""FastAPI app: upload a protocol, stream graph execution over SSE, approve the HITL gate.

Error contract: domain exceptions (app/exceptions.py) map to status codes in
one handler — clients get a JSON body, never a stack trace. The SSE stream
terminates with `__error__` instead of dying silently when a node blows up.
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.exceptions import ScreenerError, ScreeningNotFoundError
from app.graph.builder import build_graph
from app.graph.state import initial_state
from app.health import app_version, readiness
from app.logging_config import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.persistence import Persistence, ScreeningStore, open_persistence
from app.services.pdf import extract_eligibility_text

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
    graph = build_graph(_persistence.checkpointer)
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


async def _require_thread(thread_id: str) -> RunnableConfig:
    if not await _store().exists(thread_id):
        raise ScreeningNotFoundError(f"No screening found for thread_id {thread_id}")
    return {"configurable": {"thread_id": thread_id}}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _status_from_snapshot(snapshot: StateSnapshot) -> str:
    """Coarse, list-friendly status denormalized from the graph's own state.

    Kept in the store so `GET /api/screenings` never has to load every
    checkpoint just to render a status column.
    """
    if snapshot.next:
        return "awaiting_approval"
    step = snapshot.values.get("current_step")
    return str(step) if step else "done"


def _terminal_event(snapshot: StateSnapshot) -> str:
    """Translate the graph's final state into a terminal SSE event.

    A node that absorbed a failure (parser LLM-outage / unrepairable output)
    ends the run with current_step="failed" *without* raising, so it reaches
    here rather than the except blocks below. Surface it as __error__ so the
    UI shows a real failure instead of a silently-successful empty screening.
    """
    if snapshot.next:
        return _sse({"node": "__interrupt__"})
    if snapshot.values.get("current_step") == "failed":
        events = snapshot.values.get("events") or []
        message = events[-1]["detail"] if events else "Screening failed."
        return _sse({"node": "__error__", "message": message})
    return _sse({"node": "__end__"})


@app.post("/api/screenings")
async def create_screening(file: UploadFile) -> dict:
    raw = await file.read()
    if file.filename and file.filename.lower().endswith(".pdf"):
        text = extract_eligibility_text(raw)
    else:
        text = raw.decode("utf-8", errors="replace")

    thread_id = str(uuid4())
    bind_contextvars(thread_id=thread_id)
    # Persist the input durably so a restart between upload and stream — or a
    # second worker handling the stream — loses nothing. The graph's execution
    # state is rebuilt from initial_state() when the run first streams.
    await _store().create(thread_id, file.filename or "upload", text)
    # PHI hygiene: log the size of the upload, never its contents.
    log.info(
        "screening.created",
        source_filename=file.filename or "upload",
        text_chars=len(text),
    )
    return {"thread_id": thread_id}


@app.get("/api/screenings")
async def list_screenings() -> list[dict]:
    """All screenings, newest first — backs the dashboard list view."""
    records = await _store().list()
    return [
        {
            "thread_id": r.thread_id,
            "source_filename": r.source_filename,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in records
    ]


@app.get("/api/screenings/{thread_id}/stream")
async def stream_screening(thread_id: str) -> StreamingResponse:
    config = await _require_thread(thread_id)
    bind_contextvars(thread_id=thread_id)
    screening_input = await _store().get_input(thread_id)
    assert screening_input is not None  # exists() just passed
    log.info("screening.stream_started")

    async def generate() -> AsyncIterator[str]:
        # An exception mid-stream can't become an HTTP error (headers are
        # already sent) — the terminal __error__ event is the error channel,
        # and it must catch everything or the frontend hangs forever.
        try:
            state = initial_state(
                screening_input.raw_protocol_text, screening_input.source_filename
            )
            async for chunk in _graph().astream(state, config, stream_mode="updates"):
                for node, update in chunk.items():
                    yield _sse({"node": node, "update": jsonable_encoder(update)})
            snapshot = await _graph().aget_state(config)
            await _store().set_status(thread_id, _status_from_snapshot(snapshot))
            yield _terminal_event(snapshot)
            log.info("screening.stream_finished")
        except ScreenerError as exc:
            log.warning("screening.stream_error", error=type(exc).__name__, detail=str(exc))
            await _store().set_status(thread_id, "failed")
            yield _sse({"node": "__error__", "message": str(exc)})
        except Exception:  # noqa: BLE001 — last-resort stream terminator, detail stays server-side
            log.error("screening.stream_crashed", exc_info=True)
            await _store().set_status(thread_id, "failed")
            yield _sse(
                {
                    "node": "__error__",
                    "message": "Screening failed unexpectedly — check server logs.",
                }
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/screenings/{thread_id}/approve")
async def approve_screening(thread_id: str) -> dict:
    config = await _require_thread(thread_id)
    bind_contextvars(thread_id=thread_id)
    if not (await _graph().aget_state(config)).next:
        raise HTTPException(409, "screening is not awaiting approval")
    log.info("screening.approved")
    # Resume past the interrupt_before=["matcher"] gate. A DataStoreError from
    # the matcher propagates to screener_error_handler (503) and the screening
    # stays parked at the gate, so approval can be retried once fixed.
    result = await _graph().ainvoke(None, config)
    await _store().set_status(thread_id, str(result.get("current_step") or "done"))
    return {
        "matched_patients": result["matched_patients"],
        "events": jsonable_encoder(result["events"]),
    }


@app.get("/api/screenings/{thread_id}/state")
async def get_state(thread_id: str) -> dict:
    config = await _require_thread(thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await _graph().aget_state(config)
    return {"values": jsonable_encoder(snapshot.values), "pending": list(snapshot.next)}
