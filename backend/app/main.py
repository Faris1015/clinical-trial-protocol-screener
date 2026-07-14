"""FastAPI app: upload a protocol, stream graph execution over SSE, approve the HITL gate.

Error contract: domain exceptions (app/exceptions.py) map to status codes in
one handler — clients get a JSON body, never a stack trace. The SSE stream
terminates with `__error__` instead of dying silently when a node blows up.
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.runnables import RunnableConfig
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.exceptions import ScreenerError, ScreeningNotFoundError
from app.graph.builder import graph
from app.logging_config import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.services.pdf import extract_eligibility_text

# Resolve settings at import time so a misconfigured deployment fails at
# startup (e.g. LLM_PROVIDER=anthropic without ANTHROPIC_API_KEY), not
# mid-screening. configure_logging() re-applies settings-driven config (it also
# runs on logging_config import, so module-level loggers are already wired).
settings = get_settings()
configure_logging()
log = get_logger("api")

app = FastAPI(title="Clinical Trial Protocol Screener")
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
    started = time.perf_counter()
    log.info("request.start", method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
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
    "is the server alive". Dependency readiness (LLM, rules, data, DB) belongs
    to a separate /ready endpoint — see #6.
    """
    return {"status": "ok"}


# thread_id -> initial state; the graph checkpointer holds execution state
THREADS: dict[str, dict] = {}


def _require_thread(thread_id: str) -> RunnableConfig:
    if thread_id not in THREADS:
        raise ScreeningNotFoundError(f"No screening found for thread_id {thread_id}")
    return {"configurable": {"thread_id": thread_id}}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _terminal_event(config: RunnableConfig) -> str:
    """Translate the graph's final state into a terminal SSE event.

    A node that absorbed a failure (parser LLM-outage / unrepairable output)
    ends the run with current_step="failed" *without* raising, so it reaches
    here rather than the except blocks below. Surface it as __error__ so the
    UI shows a real failure instead of a silently-successful empty screening.
    """
    snapshot = graph.get_state(config)
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
    THREADS[thread_id] = {
        "raw_protocol_text": text,
        "source_filename": file.filename or "upload",
        "parsed_criteria": None,
        "compliance_passed": False,
        "critic_feedback": None,
        "parse_attempts": 0,
        "compliance_findings": [],
        "matched_patients": [],
        "events": [],
        "current_step": "routing",
    }
    # PHI hygiene: log the size of the upload, never its contents.
    log.info(
        "screening.created",
        source_filename=file.filename or "upload",
        text_chars=len(text),
    )
    return {"thread_id": thread_id}


@app.get("/api/screenings/{thread_id}/stream")
async def stream_screening(thread_id: str) -> StreamingResponse:
    config = _require_thread(thread_id)
    bind_contextvars(thread_id=thread_id)
    log.info("screening.stream_started")

    async def generate() -> AsyncIterator[str]:
        # An exception mid-stream can't become an HTTP error (headers are
        # already sent) — the terminal __error__ event is the error channel,
        # and it must catch everything or the frontend hangs forever.
        try:
            async for chunk in graph.astream(THREADS[thread_id], config, stream_mode="updates"):
                for node, update in chunk.items():
                    yield _sse({"node": node, "update": jsonable_encoder(update)})
            yield _terminal_event(config)
            log.info("screening.stream_finished")
        except ScreenerError as exc:
            log.warning("screening.stream_error", error=type(exc).__name__, detail=str(exc))
            yield _sse({"node": "__error__", "message": str(exc)})
        except Exception:  # noqa: BLE001 — last-resort stream terminator, detail stays server-side
            log.error("screening.stream_crashed", exc_info=True)
            yield _sse(
                {
                    "node": "__error__",
                    "message": "Screening failed unexpectedly — check server logs.",
                }
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/screenings/{thread_id}/approve")
async def approve_screening(thread_id: str) -> dict:
    config = _require_thread(thread_id)
    bind_contextvars(thread_id=thread_id)
    if not graph.get_state(config).next:
        raise HTTPException(409, "screening is not awaiting approval")
    log.info("screening.approved")
    # Resume past the interrupt_before=["matcher"] gate. A DataStoreError from
    # the matcher propagates to screener_error_handler (503) and the screening
    # stays parked at the gate, so approval can be retried once fixed.
    result = graph.invoke(None, config)
    return {
        "matched_patients": result["matched_patients"],
        "events": jsonable_encoder(result["events"]),
    }


@app.get("/api/screenings/{thread_id}/state")
async def get_state(thread_id: str) -> dict:
    config = _require_thread(thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = graph.get_state(config)
    return {"values": jsonable_encoder(snapshot.values), "pending": list(snapshot.next)}
