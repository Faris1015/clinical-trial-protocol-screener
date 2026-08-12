"""FastAPI app: HTTP edge for TrialGate — routing, error contract, logging.

Routes are thin translators: they read the request, resolve the wired
dependencies (store, graph), and hand off to `app.services.screening`, which
owns all screening business logic. Nothing here builds state, invokes the
graph, or formats SSE frames directly.

Error contract: domain exceptions (app/exceptions.py) map to status codes in
one handler — clients get a JSON body, never a stack trace. The SSE stream
terminates with `__error__` instead of dying silently when a node blows up.

Auth (#50): every `/api` route except `/api/auth/*` carries an explicit
`Depends(require_reviewer|require_admin)` guard, so the requirement is visible in
the signature and the handler holds the Principal it needs for the audit trail. A
test asserts that coverage, so a new route can't ship unguarded by omission.
"""

import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.graph.state import CompiledStateGraph
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, StringConstraints, model_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import (
    Principal,
    Role,
    authenticate,
    configured_users,
    issue_session,
    require_admin,
    require_reviewer,
    session_secret,
)
from app.config import get_settings
from app.exceptions import (
    InvalidBatchError,
    InvalidCredentialsError,
    PayloadTooLargeError,
    ScreenerError,
)
from app.health import app_version, readiness
from app.logging_config import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.persistence import Persistence, ScreeningStore, open_persistence
from app.schemas.criteria import CriteriaSchema
from app.services import rules, screening, simulation, sse
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

# Runs-index paging (#51). The ceiling is the point: without one, `?limit=100000`
# turns a cheap read into a full table scan serialized into a single response.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# How many protocols one batch submission may carry (#61). The cap is the point:
# the multipart body is parsed before a handler runs, so an uncapped `list[UploadFile]`
# would let one request spool an arbitrary number of documents to disk and then hand
# the pipeline an arbitrary amount of PDF extraction to do. Ten covers the realistic
# case — a coordinator dropping in a trial's worth of protocols — and a larger set is
# two submissions.
MAX_BATCH_FILES = 10

# Ceiling on an edited extraction (#53), measured on its JSON form. Unlike an
# upload — which `read_upload_capped` bounds — a JSON body has no size gate of its
# own, and this payload is written straight into a checkpoint that every
# subsequent read of the run has to load. A real protocol's eligibility section
# serializes to a few KB; 256 KiB is generous for a human-edited one and still
# refuses to persist a megabyte of junk.
MAX_CRITERIA_EDIT_BYTES = 256 * 1024

# Ceiling on a rejection reason (#91). Same reasoning as the edit cap on a much
# smaller payload: this is one sentence of free text written into a checkpoint and
# rendered in every view of the run, so it is bounded rather than trusted. Long
# enough for a paragraph of clinical justification, short enough that no reviewer
# can turn the audit trail into a document store.
MAX_REJECTION_REASON_CHARS = 2000

# How many thresholds one what-if may move (#95). A protocol's eligibility section
# rarely carries more than a handful of numeric criteria, and the simulation is
# linear in overrides × patients — so this is comfortably above any real request
# and still refuses a client that tries to turn one POST into a sweep.
MAX_SIMULATION_OVERRIDES = 20

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


app = FastAPI(title="TrialGate", lifespan=lifespan)
# slowapi reads the limiter off app.state and its handler turns a tripped limit
# into a 429 (with Retry-After) that our error contract shape wraps below.
app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
    # The session lives in a cookie (#50), so a cross-origin browser call has to
    # be allowed to send it. Safe here only because allow_origins is an explicit
    # allowlist — credentialed CORS with a wildcard origin is rejected by browsers
    # and would be a hole if it weren't. Every deployed topology is same-origin
    # anyway; this covers hitting the API directly from a dev page.
    allow_credentials=True,
)

# Standard HTTP metrics (request count, latency histogram, in-flight) at
# GET /metrics (#7). Custom domain metrics live in app/services/metrics.py and
# register on the same default registry, so one scrape returns both. Excluded
# from the OpenAPI schema and the access log (see _QUIET_PATHS) — it's an
# operator endpoint, not part of the API contract.
if settings.metrics_enabled:
    # The exclusions are regexes matched with `re.search`, so they are anchored:
    # bare "/metrics" also matches "/api/metrics/summary" (#58), which would drop
    # a real API route out of the HTTP metrics for looking like the scrape
    # endpoint. Three operator paths are meant here, exactly.
    Instrumentator(excluded_handlers=[r"^/metrics$", r"^/health$", r"^/ready$"]).instrument(
        app
    ).expose(app, endpoint="/metrics", include_in_schema=False)


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


# --- Auth (#50) -------------------------------------------------------------
#
# The frontend is a static export with no request-time server (see
# frontend/next.config.ts), so FastAPI — not NextAuth — is the identity
# authority. Design notes, including why the session is a cookie and how CSRF is
# handled, are in app/auth.py.


class LoginRequest(BaseModel):
    # Surrounding whitespace is tolerated (pasted credentials routinely carry it)
    # but the value must then look like an address with no interior whitespace or
    # control characters. That rejects junk with a 422 before it costs a scrypt
    # pass, and — since the failed-login log line echoes this value — it closes
    # the same log-forging hole `_REQUEST_ID_RE` closes for X-Request-ID.
    email: Annotated[
        str,
        StringConstraints(strip_whitespace=True, pattern=r"^[^\s@]+@[^\s@]+$", max_length=320),
    ]
    # Bounded so a login attempt can't hand scrypt a huge input to chew on.
    password: str = Field(max_length=1024)


class PrincipalResponse(BaseModel):
    """The caller's identity — what the frontend gates its UI on."""

    email: str
    role: Role


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=settings.auth_session_ttl_seconds,
        # httponly: the token is never readable by JS, so an XSS foothold can't
        # exfiltrate a reusable session.
        httponly=True,
        # samesite=strict is the CSRF defense for the state-changing routes below;
        # every deployed topology is same-origin, so nothing legitimate needs a
        # cross-site cookie. See app/auth.py.
        samesite="strict",
        secure=settings.auth_cookie_secure,
        path="/",
    )


@app.post("/api/auth/login")
@limiter.limit(lambda: settings.rate_limit_login)
async def login(
    request: Request, credentials: LoginRequest, response: Response
) -> PrincipalResponse:
    """Exchange credentials for a session cookie.

    Rate-limited per IP (`RATE_LIMIT_LOGIN`) because this is the one endpoint an
    attacker can guess against. The cookie is set on the injected `response`
    (which FastAPI merges into the real one) so the return type stays the declared
    model rather than an opaque Response.
    """
    principal = authenticate(credentials.email, credentials.password, configured_users(settings))
    if principal is None:
        # One message for both "no such user" and "wrong password" — the response
        # must not confirm that an email is registered.
        log.warning("auth.login_failed", email=credentials.email)
        raise InvalidCredentialsError("Incorrect email or password.")
    token = issue_session(principal, session_secret(settings), settings.auth_session_ttl_seconds)
    log.info("auth.login", email=principal.email, role=principal.role)
    _set_session_cookie(response, token)
    return PrincipalResponse(email=principal.email, role=principal.role)


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    """Clear the session cookie.

    Unauthenticated on purpose: signing out of an already-expired session must
    still clear the stale cookie rather than 401.
    """
    # Attributes must match the ones the cookie was set with, or the browser
    # treats it as a different cookie and leaves the original in place.
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.auth_cookie_secure,
    )
    return {"status": "signed_out"}


@app.get("/api/auth/me")
async def me(principal: Annotated[Principal, Depends(require_reviewer)]) -> PrincipalResponse:
    """Who the caller is — the frontend's session bootstrap and role source."""
    return PrincipalResponse(email=principal.email, role=principal.role)


@app.get("/api/admin/users")
@limiter.limit(lambda: settings.rate_limit_read)
async def list_users(
    request: Request, principal: Annotated[Principal, Depends(require_admin)]
) -> list[PrincipalResponse]:
    """Configured accounts — admin only, and never their password hashes.

    The user-management half of the admin role. The compliance rules are readable
    by any reviewer (`GET /api/rules`, #57); writing them would land here.
    It is also what gives the role ladder a real 403 to enforce: a reviewer
    hitting this gets Forbidden, and the frontend hides the nav entry for them.
    """
    users = configured_users(settings)
    return [
        PrincipalResponse(email=user.email, role=user.role)
        for user in sorted(users.values(), key=lambda u: u.email)
    ]


# --- Screenings -------------------------------------------------------------


def _reject_oversized_body(request: Request, max_bytes: int) -> None:
    """Turn away an upload from its *declared* size, before anything reads it.

    A 100 MB spam POST is refused in well under a second this way — nothing here
    parses, extracts or persists it. It is a fast path, not the guard:
    `read_upload_capped` is what bounds a spoofed or absent Content-Length, per
    file. The slack covers the multipart framing (boundaries, part headers) that
    rides along with the file bytes.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + 8192:
        raise PayloadTooLargeError(
            f"Upload exceeds the {max_bytes} byte limit.",
            headers={"Connection": "close"},
        )


@app.post("/api/screenings")
@limiter.limit(lambda: settings.rate_limit_create)
async def create_screening(
    request: Request, file: UploadFile, principal: Annotated[Principal, Depends(require_reviewer)]
) -> dict:
    _reject_oversized_body(request, settings.max_upload_bytes)
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


@app.post("/api/screenings/batch")
@limiter.limit(lambda: settings.rate_limit_create)
async def create_screening_batch(
    request: Request,
    principal: Annotated[Principal, Depends(require_reviewer)],
    files: list[UploadFile],
) -> dict:
    """Screen several protocols in one submission (#61) — one thread per file.

    Not a loop the client could run itself over `POST /api/screenings`: that route
    is rate limited per request (`RATE_LIMIT_CREATE`, 10/minute), so a coordinator
    with a folder of protocols would trip the limiter on the tenth file and have
    the rest of their batch silently refused. One submission is one request against
    that budget, and `MAX_BATCH_FILES` — not the limiter — is what bounds it.

    Answers 200 with a per-file breakdown even when some files were rejected;
    `services/screening.create_screening_batch` documents why partial success is
    the contract here. The whole submission is refused (422) only for the file
    *set* being wrong, and 413 for a body over the aggregate cap.

    The created screenings are not started here. Each runs when a client streams
    it, exactly as a single upload does, so the concurrency gate stays in charge of
    how many graph runs are in flight (the frontend's batch view streams them a
    couple at a time).
    """
    # A submission with no `files` part at all is already a 422 from FastAPI's own
    # body validation, so the only file-set rule left to state is the ceiling.
    if len(files) > MAX_BATCH_FILES:
        raise InvalidBatchError(
            f"A batch carries at most {MAX_BATCH_FILES} protocols; this one has {len(files)}. "
            "Split it into smaller submissions."
        )
    # Every file is capped individually inside the service; this bounds the
    # submission as a whole, so `MAX_BATCH_FILES` full-size protocols is the most
    # one request can ever be.
    _reject_oversized_body(request, settings.max_upload_bytes * MAX_BATCH_FILES)
    return await screening.create_screening_batch(
        _store(),
        files,
        allowed_content_types=settings.upload_content_type_set,
        max_upload_bytes=settings.max_upload_bytes,
        max_pdf_pages=settings.max_pdf_pages,
        max_text_chars=settings.max_protocol_text_chars,
    )


@app.get("/api/screenings")
@limiter.limit(lambda: settings.rate_limit_read)
async def list_screenings(
    request: Request,
    principal: Annotated[Principal, Depends(require_reviewer)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[screening.ScreeningStatus | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    """The runs index (#51): one page of past screenings, newest first.

    Returns `{items, total, limit, offset}` — a bare list can't say whether more
    rows matched than were returned. `status` is validated against the phases a
    screening can actually be in, so a typo is a 422 rather than a silently
    empty page, and `q` is a case-insensitive substring match on filename or
    thread_id.
    """
    return await screening.list_screenings(
        _store(), limit=limit, offset=offset, status=status, search=q
    )


@app.get("/api/screenings/compare")
@limiter.limit(lambda: settings.rate_limit_read)
async def compare_screenings(
    request: Request,
    principal: Annotated[Principal, Depends(require_reviewer)],
    a: Annotated[str, Query(min_length=1, max_length=200)],
    b: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict:
    """Two runs side by side — criteria and cohort verdicts diffed (#59).

    A collection-level route with both runs in the query string rather than
    `/{thread_id}/compare/{other}`: a comparison is symmetric and belongs to
    neither run, and nesting it under one of them would imply an owner. It cannot
    collide with the `/{thread_id}/…` routes below — those carry a second path
    segment, `compare` does not.

    Both ids are declared as plain strings with a length cap, not a checked format:
    a thread id is minted as a UUID here, but the store is the authority on what
    exists, and validating the shape would turn an unknown id's clear 404 into a
    422 about characters. `a == b` is a 422 (see services.screening).
    """
    return await screening.compare_screenings(_store(), _graph(), a, b)


@app.get("/api/screenings/{thread_id}/stream")
@limiter.limit(lambda: settings.rate_limit_read)
async def stream_screening(
    request: Request,
    thread_id: str,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> StreamingResponse:
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
async def approve_screening(
    request: Request,
    thread_id: str,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> StreamingResponse:
    # Mirror the stream route: the matcher can run for minutes on a local model,
    # so hold a concurrency slot for its lifetime and stream its progress rather
    # than blocking the POST until the whole cohort is scored (which times out
    # the client and provokes duplicate approve clicks). Eager validation inside
    # approve_screening raises before the response commits, so the slot acquired
    # here is released on that path too.
    active_screenings.acquire()
    try:
        frames = await screening.approve_screening(_store(), _graph(), thread_id, principal)
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


class RejectionRequest(BaseModel):
    """A reviewer's decision to stop a screening, and why (#91).

    The reason is required and stripped of surrounding whitespace, so a rejection
    can never be recorded as a blank string: this is the one field that tells
    whoever reads the run later *why* a protocol was refused, and a terminal state
    with no explanation is exactly the dead end this endpoint exists to prevent.
    """

    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=MAX_REJECTION_REASON_CHARS
        ),
        Field(description="Why this protocol is not screenable. Recorded in the audit trail."),
    ]


@app.post("/api/screenings/{thread_id}/reject")
@limiter.limit(lambda: settings.rate_limit_create)
async def reject_screening(
    request: Request,
    thread_id: str,
    rejection: RejectionRequest,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> dict:
    """Reject a screening at the human gate — the decision, on the record (#91).

    Guarded by `require_reviewer` exactly as approval is: the two answers to the
    same question carry the same authority, and who gave either one is the audit
    trail's whole point.

    Unlike approve and edit-and-rerun, this returns JSON rather than a stream and
    takes no concurrency slot. Those two resume the graph — minutes of LLM work
    whose progress has to reach the client — while a rejection runs nothing: it
    writes the decision, terminates the run, and is done. Rate limited with the
    write bucket all the same, since it does persist.
    """
    return await screening.reject_screening(
        _store(), _graph(), thread_id, principal, rejection.reason
    )


class CriteriaEditRequest(BaseModel):
    """A reviewer's corrected extraction, plus the revision it was made against (#53).

    The whole `CriteriaSchema` is submitted rather than a patch of individual
    fields: it is the same contract the Parser produces, so Pydantic validates a
    hand-edited criterion exactly as strictly as a generated one — a threshold on
    an attribute outside the closed EHR vocabulary, or an operator the Matcher
    can't apply, is a 422 here instead of a broken run later.
    """

    base_revision: int = Field(
        ge=0,
        description="The criteria_revision these edits were made against; a mismatch is a 409.",
    )
    criteria: CriteriaSchema

    @model_validator(mode="after")
    def _within_size_cap(self) -> "CriteriaEditRequest":
        if len(self.criteria.model_dump_json()) > MAX_CRITERIA_EDIT_BYTES:
            raise ValueError(f"Edited criteria exceed the {MAX_CRITERIA_EDIT_BYTES} byte limit.")
        return self


@app.patch("/api/screenings/{thread_id}/criteria")
@limiter.limit(lambda: settings.rate_limit_create)
async def edit_criteria(
    request: Request,
    thread_id: str,
    edits: CriteriaEditRequest,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> StreamingResponse:
    """Correct the parsed criteria at the human gate and re-run from the Critic.

    Mirrors the approve route's shape — a concurrency slot held for the run's
    lifetime and an SSE stream rather than a blocking POST — because the resume
    re-runs the Critic, whose semantic review is an LLM call that can take as long
    as the pre-gate phase it belongs to (hence the same idle timeout, not the
    matcher's longer one). Eager validation inside the service raises before the
    response commits, so the slot is released on that path too.
    """
    active_screenings.acquire()
    try:
        frames = await screening.resume_with_edited_criteria(
            _store(),
            _graph(),
            thread_id,
            criteria=edits.criteria.model_dump(),
            base_revision=edits.base_revision,
            editor=principal,
        )
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


class CriterionOverrideRequest(BaseModel):
    """One criterion's threshold as a what-if would have it (#95).

    Only the comparison moves. The attribute and the unit are what a criterion is
    *about*, and changing either is an edit to the extraction rather than a
    question about its bound — which is why this is not a `QuantitativeCriterion`:
    accepting one would let a simulation quietly re-point a criterion at a
    different lab and report the result as the same criterion relaxed.

    The bounds are plain floats, and finiteness is checked in the service rather
    than here — `services/simulation._resolve` documents why declaring
    `allow_inf_nan=False` turns a NaN body into a 500 instead of a 422.
    """

    key: str = Field(
        min_length=1,
        max_length=500,
        description="The criterion's attrition key, as shown on the cohort panel.",
    )
    operator: Literal[">=", "<=", ">", "<", "==", "between"]
    value: float
    value_high: float | None = None


class SimulationRequest(BaseModel):
    """The set of thresholds to move for one what-if.

    Bounded at both ends. At least one override, because simulating nothing is a
    request for the cohort the caller already has; at most `MAX_SIMULATION_OVERRIDES`,
    because the work is linear in overrides × patients and a protocol has nowhere
    near that many numeric criteria — a larger list is a client bug, not a reviewer.
    """

    overrides: list[CriterionOverrideRequest] = Field(
        min_length=1, max_length=MAX_SIMULATION_OVERRIDES
    )


@app.post("/api/screenings/{thread_id}/simulate")
@limiter.limit(lambda: settings.rate_limit_read)
async def simulate_screening(
    request: Request,
    thread_id: str,
    body: SimulationRequest,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> dict:
    """What the cohort would look like under different thresholds (#95).

    A POST because the overrides are a structured body, not because anything is
    written: this reads the checkpoint and returns a projection, and the service
    never calls `aupdate_state` on this path. Hence the *read* rate-limit bucket
    and no concurrency slot — there is no graph run to bound, no LLM call to make
    (see `services/simulation.py`), and a reviewer dragging a threshold expects the
    numbers to keep up.

    Reviewer-guarded like every other `/api` route. The rung matters less here than
    it does on `/approve`: a simulation reaches no further into patient data than
    `GET /state` already does, and it is read-only for every role, so the guard is
    about who may read the run rather than about who may change it.
    """
    return await screening.simulate_screening(
        _store(),
        _graph(),
        thread_id,
        [
            # Named rather than `model_dump()`ed: the service's input is a closed
            # four-field contract, so a field added to the request model has to be
            # threaded through deliberately instead of arriving as a surprise key.
            simulation.Override(
                key=override.key,
                operator=override.operator,
                value=override.value,
                value_high=override.value_high,
            )
            for override in body.overrides
        ],
    )


@app.get("/api/screenings/{thread_id}/state")
@limiter.limit(lambda: settings.rate_limit_read)
async def get_state(
    request: Request,
    thread_id: str,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> dict:
    return await screening.get_screening_state(_store(), _graph(), thread_id)


@app.get("/api/screenings/{thread_id}/protocol")
@limiter.limit(lambda: settings.rate_limit_read)
async def get_protocol(
    request: Request,
    thread_id: str,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> dict:
    """The uploaded protocol text and where each criterion came from in it (#54).

    Returned as JSON rather than as the original upload: the payload carries the
    resolved `spans` alongside the text, and the two have to describe the same
    string for a highlight to land on the right passage. The text is rendered as
    DOM text nodes by the viewer, never as markup — which is what keeps an
    untrusted upload inert without needing the report route's download headers.
    """
    return await screening.get_screening_protocol(_store(), _graph(), thread_id)


@app.get("/api/screenings/{thread_id}/report")
@limiter.limit(lambda: settings.rate_limit_read)
async def download_report(
    request: Request,
    thread_id: str,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> Response:
    """Download one run as a self-contained HTML report (#56).

    Three response headers carry weight beyond the download itself. The document
    is rendered from a protocol an untrusted party uploaded and an LLM rewrote, and
    in the demo topology this API shares an origin with the app — so even though
    `services/report.py` escapes every interpolation, the response is pinned down
    as a file rather than a page: `attachment` makes the browser save it instead of
    rendering it in our origin, `nosniff` stops the declared type being second-
    guessed, and the CSP means that a reference-free document stays reference-free
    (no script, no network, no frame) if a reader opens it anyway. The report links
    to nothing and loads nothing, so the policy costs it nothing.

    `principal` is not only the guard here: the report carries patient data out of
    the app, so the service logs who exported it.
    """
    filename, document = await screening.get_screening_report(
        _store(), _graph(), thread_id, principal
    )
    return Response(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={
            # `report_filename` emits only [A-Za-z0-9._-] (it runs the stored name
            # back through `sanitize_filename`), so the quoted form needs no
            # further escaping and cannot inject a header.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


# What each export format is served as. The CSV type is `text/csv` with an
# explicit charset — the BOM `services/export.py` writes tells Excel, and the
# header tells everything else.
_EXPORT_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


@app.get("/api/screenings/{thread_id}/export")
@limiter.limit(lambda: settings.rate_limit_read)
async def download_cohort_export(
    request: Request,
    thread_id: str,
    principal: Annotated[Principal, Depends(require_reviewer)],
    format: Annotated[Literal["csv", "json"], Query()] = "csv",
) -> Response:
    """Download one run's evaluated cohort as CSV or JSON (#102).

    The report route's sibling, and guarded at the same rung on purpose: this
    carries the same patient data out of the app in a different wrapper, so a role
    that may not read the report must not be able to read the cohort by asking for
    it as a spreadsheet. `services/screening.get_screening_export` logs who took a
    copy, which is what attributes the download in the audit index (#98).

    The response headers do the same work they do on the report, for the same
    reason: `attachment` keeps a document built from an uploaded protocol from
    rendering as a page in our own origin, `nosniff` stops the declared type being
    second-guessed (a CSV sniffed as HTML is exactly the hole the disposition
    closes), and the CSP costs a reference-free file nothing.

    `format` is a `Literal`, so an unknown value is a 422 from FastAPI's own
    validation rather than a silent fallback to CSV — a client asking for a format
    this build does not serve should be told, not handed a different file.
    """
    filename, body = await screening.get_screening_export(
        _store(), _graph(), thread_id, format, principal
    )
    return Response(
        content=body,
        media_type=_EXPORT_MEDIA_TYPES[format],
        headers={
            # `export_filename` emits only [A-Za-z0-9._-] (it runs the stored name
            # back through `sanitize_filename`), so the quoted form needs no
            # further escaping and cannot inject a header.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@app.get("/api/rules")
@limiter.limit(lambda: settings.rate_limit_read)
async def list_compliance_rules(
    request: Request,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> dict:
    """The compliance rules the Critic checks every extraction against (#57).

    Read-only, and served rather than bundled into the frontend: the rules file is
    deployment configuration (`RULES_PATH`), so an instance running amended rules
    must show the rules it is actually running, not the ones that were in the repo
    when the bundle was built.

    Reviewer-guarded like every other read. Nothing here is patient data, but the
    thresholds are this deployment's compliance posture and there is no reason to
    hand them to an unauthenticated caller.
    """
    return rules.list_compliance_rules()


@app.get("/api/metrics/summary")
@limiter.limit(lambda: settings.rate_limit_read)
async def metrics_overview(
    request: Request,
    principal: Annotated[Principal, Depends(require_reviewer)],
) -> dict:
    """The domain metrics as an in-app summary — funnel, rejections, depth (#58),
    and how much of the protocols this instance saw it could actually check (#93).

    Distinct from `/metrics` in audience, not in data: the first three read the
    same collectors that endpoint serializes (see services/metrics_summary.py), so
    the two cannot disagree. Coverage is the one figure here a Prometheus counter
    could not carry — ranking the phrasings the vocabulary cannot parse means
    reading the sentences, so it is a bounded walk over recent checkpoints (see
    services/screening.get_metrics_summary). Under `/api` and reviewer-guarded
    because it is part of the app, while `/metrics` stays unauthenticated for the
    scraper and out of the OpenAPI schema.

    Reviewer-guarded rather than admin: these are aggregates over the pipeline's
    own behaviour, with no patient data and no per-run detail, and the reviewers
    being asked to act on escalations are the people who should see how often they
    happen. Coverage adds protocol text — the `unparseable` phrasings — which is
    the same class of data `/api/rules` and every criteria view already serve to
    this rung.
    """
    return await screening.get_metrics_summary(_store(), _graph())


def mount_frontend(app: FastAPI, dist: Path | None) -> bool:
    """Single-service demo mode: serve the built frontend bundle from this app.

    When `dist` points at a directory containing index.html, mount it at "/" so
    one container hosts the whole demo (frontend + API, same origin, no CORS).
    Must be called AFTER every API/operator route is registered: the catch-all
    mount is matched last, so those routes always win. `html=True` then gives us
    exactly what the frontend's static export needs — index.html at "/", the
    hashed assets, `<route>/index.html` for each non-root route (Next writes one
    directory per route), and 404.html for an unknown path. Returns whether it
    mounted. A no-op in the split production topology (dist unset — nginx serves
    the bundle there). See deploy/demo/Dockerfile, docs/free-demo-deploy.md.
    """
    if not (dist and (dist / "index.html").is_file()):
        return False
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    log.info("frontend.mounted", path=str(dist))
    return True


mount_frontend(app, settings.frontend_dist)
