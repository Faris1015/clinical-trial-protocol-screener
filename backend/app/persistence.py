"""Durable persistence for screenings (#2).

Three things must survive a restart, crash, or deploy:

1. **LangGraph execution state** — the checkpointer. A run parked at the
   human-approval gate can wait hours; losing it drops the screening.
2. **Screening metadata** — thread_id, filename, status, created_at, the
   denormalized criteria/match counts, plus the uploaded protocol text (the
   input a run streams from). This is what the runs index lists and what a
   delayed `/stream` rebuilds its input from.
3. **The decision index** — every approval, rejection, criteria revision and
   escalation, across every run, appended as it happens (#98). Its own table and
   its own repository (``AuditStore``) rather than a column on the row above: a
   screening has one status and many decisions, and the org-wide question an
   auditor asks — *what did this person do last quarter* — is not a question
   about any one run.

All three live in the *same* database, selected by ``CHECKPOINT_BACKEND``:

- ``memory``   — process-local, lost on restart (tests only).
- ``sqlite``   — durable single-node default.
- ``postgres`` — multi-replica production target (deps in the ``postgres`` extra).

Route handlers never touch SQL: they call the ``ScreeningStore`` /
``AuditStore`` repositories. Nothing here is constructed at import —
``AsyncSqliteSaver`` captures the running loop in its constructor, so everything
is built inside ``open_persistence`` from the app's lifespan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from app.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite
    from psycopg import AsyncConnection

    from app.config import Settings

log = get_logger("persistence")


@dataclass(frozen=True)
class ScreeningRecord:
    """Metadata row for the list view — never carries the protocol text.

    ``criteria_count`` and ``match_count`` are denormalized from the graph state
    when a run reaches a terminal frame (see ``services.screening``). They live
    here for the same reason ``status`` does: the runs index (#51) renders them
    for every row, and reading them from the checkpoints would mean loading one
    per screening on every page view.

    ``coverage_checkable``/``coverage_criteria`` are the screenability score (#93)
    denormalized the same way, and by the same writer — how many of the criteria
    the extraction produced this run could actually check, over how many there
    were. Stored as the pair rather than as a percentage so the API recombines
    them through ``services.coverage.score_of``: the formula stays in one place,
    and a row can still say *"14 of 20"* rather than only *"70%"*. Both are 0 for
    a run whose parse never landed, which is how the index tells "nothing to
    score" from "nothing was checkable".

    ``llm_tokens``/``llm_cost_micro_usd`` are the run's LLM bill (#101),
    denormalized by the same writer for the same reason. Cost is stored in
    *micro-USD as an integer* rather than as a float: a screening costs cents, and
    ``services.usage.usd`` is the one conversion back to dollars — so the runs
    index, the run detail view and the exported figure are one derivation read
    several times. Both are 0 for a run that made no LLM call.
    """

    thread_id: str
    source_filename: str
    status: str
    created_at: str
    criteria_count: int = 0
    match_count: int = 0
    coverage_checkable: int = 0
    coverage_criteria: int = 0
    llm_tokens: int = 0
    llm_cost_micro_usd: int = 0


@dataclass(frozen=True)
class ScreeningPage:
    """One page of ``list()`` results plus the total the filter matched.

    ``total`` counts every row matching the status/search filter, not just the
    ones on this page — it is what lets the UI say "26–50 of 312" and know
    whether a next page exists.
    """

    items: list[ScreeningRecord]
    total: int


@dataclass(frozen=True)
class ScreeningInput:
    """The input a run streams from, rehydrated from the store at stream time."""

    raw_protocol_text: str
    source_filename: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Wildcards a user types into the search box are data, not syntax: without
# escaping, a filename containing "%" would match everything and "_" would match
# any character. Backslash is the escape character declared by `ESCAPE '\'` in
# the LIKE clauses below, so it has to be escaped first.
def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _list_filters(
    status: str | None, search: str | None, placeholder: str, like: str
) -> tuple[str, list[str]]:
    """Build the shared WHERE clause for ``list()``/its COUNT twin.

    Both SQL stores filter identically; only the parameter placeholder (``?`` vs
    ``%s``) and the case-insensitive LIKE spelling (sqlite's ``LIKE`` is already
    ASCII-case-insensitive, postgres needs ``ILIKE``) differ, so those are
    arguments rather than two near-identical copies of this logic.
    """
    clauses: list[str] = []
    params: list[str] = []
    if status:
        clauses.append(f"status = {placeholder}")
        params.append(status)
    if search:
        # thread_id as well as filename: a support request quotes the id, and
        # pasting it into the same box is the obvious thing to try.
        clauses.append(
            f"(source_filename {like} {placeholder} ESCAPE '\\' "
            f"OR thread_id {like} {placeholder} ESCAPE '\\')"
        )
        pattern = f"%{_escape_like(search)}%"
        params += [pattern, pattern]
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# --- Repository interface ---------------------------------------------------


class ScreeningStore(ABC):
    """Thin async repository for screening metadata + input. No ORM."""

    @abstractmethod
    async def setup(self) -> None:
        """Create tables if absent. Idempotent."""

    @abstractmethod
    async def create(
        self, thread_id: str, source_filename: str, raw_protocol_text: str
    ) -> None: ...

    @abstractmethod
    async def exists(self, thread_id: str) -> bool: ...

    @abstractmethod
    async def get_input(self, thread_id: str) -> ScreeningInput | None: ...

    @abstractmethod
    async def get_record(self, thread_id: str) -> ScreeningRecord | None:
        """One screening's metadata row.

        The authoritative status for a single run: a screening that was uploaded
        but never streamed has no checkpoint at all, so the graph cannot say what
        phase it is in — only this row can.
        """

    @abstractmethod
    async def set_status(
        self,
        thread_id: str,
        status: str,
        *,
        criteria_count: int | None = None,
        match_count: int | None = None,
        coverage_checkable: int | None = None,
        coverage_criteria: int | None = None,
        llm_tokens: int | None = None,
        llm_cost_micro_usd: int | None = None,
    ) -> None:
        """Update the denormalized run summary.

        A ``None`` count leaves that column as it was, so an error path can
        record ``status="failed"`` without erasing counts an earlier phase
        already established.
        """

    @abstractmethod
    async def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        search: str | None = None,
    ) -> ScreeningPage:
        """One page of screenings, newest first, plus the total matching the filter.

        ``search`` is a case-insensitive substring match over the filename and
        the thread_id; ``status`` is an exact match. Both are optional and
        combine with AND.
        """


class InMemoryScreeningStore(ScreeningStore):
    """Dict-backed store for tests — no durability, no I/O, no event loop needed."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    async def setup(self) -> None:
        return None

    async def create(self, thread_id: str, source_filename: str, raw_protocol_text: str) -> None:
        self._rows[thread_id] = {
            "thread_id": thread_id,
            "source_filename": source_filename,
            "raw_protocol_text": raw_protocol_text,
            "status": "routing",
            "created_at": _now(),
            "criteria_count": 0,
            "match_count": 0,
            "coverage_checkable": 0,
            "coverage_criteria": 0,
            "llm_tokens": 0,
            "llm_cost_micro_usd": 0,
        }

    async def exists(self, thread_id: str) -> bool:
        return thread_id in self._rows

    async def get_input(self, thread_id: str) -> ScreeningInput | None:
        row = self._rows.get(thread_id)
        if row is None:
            return None
        return ScreeningInput(row["raw_protocol_text"], row["source_filename"])

    @staticmethod
    def _as_record(row: dict) -> ScreeningRecord:
        """One dict row as a record — the in-memory twin of ``_record``.

        Shared by ``get_record`` and ``list`` so a column added to the dataclass
        cannot reach one of them and not the other.
        """
        return ScreeningRecord(
            row["thread_id"],
            row["source_filename"],
            row["status"],
            row["created_at"],
            row["criteria_count"],
            row["match_count"],
            row["coverage_checkable"],
            row["coverage_criteria"],
            row["llm_tokens"],
            row["llm_cost_micro_usd"],
        )

    async def get_record(self, thread_id: str) -> ScreeningRecord | None:
        row = self._rows.get(thread_id)
        return self._as_record(row) if row else None

    async def set_status(
        self,
        thread_id: str,
        status: str,
        *,
        criteria_count: int | None = None,
        match_count: int | None = None,
        coverage_checkable: int | None = None,
        coverage_criteria: int | None = None,
        llm_tokens: int | None = None,
        llm_cost_micro_usd: int | None = None,
    ) -> None:
        row = self._rows.get(thread_id)
        if row is None:
            return
        row["status"] = status
        # Mirrors the SQL stores' COALESCE: a None leaves the stored count alone,
        # so an error path can record "failed" without erasing what a finished
        # phase established.
        updates = {
            "criteria_count": criteria_count,
            "match_count": match_count,
            "coverage_checkable": coverage_checkable,
            "coverage_criteria": coverage_criteria,
            "llm_tokens": llm_tokens,
            "llm_cost_micro_usd": llm_cost_micro_usd,
        }
        for column, value in updates.items():
            if value is not None:
                row[column] = value

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        search: str | None = None,
    ) -> ScreeningPage:
        rows = sorted(self._rows.values(), key=lambda r: r["created_at"], reverse=True)
        if status:
            rows = [r for r in rows if r["status"] == status]
        if search:
            # Mirrors the SQL stores' case-insensitive filename/thread_id match.
            needle = search.lower()
            rows = [
                r
                for r in rows
                if needle in r["source_filename"].lower() or needle in r["thread_id"].lower()
            ]
        page = rows[offset : offset + limit]
        return ScreeningPage(items=[self._as_record(r) for r in page], total=len(rows))


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS screenings (
    thread_id         TEXT PRIMARY KEY,
    source_filename   TEXT NOT NULL,
    raw_protocol_text TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    criteria_count      INTEGER NOT NULL DEFAULT 0,
    match_count         INTEGER NOT NULL DEFAULT 0,
    coverage_checkable  INTEGER NOT NULL DEFAULT 0,
    coverage_criteria   INTEGER NOT NULL DEFAULT 0,
    llm_tokens          INTEGER NOT NULL DEFAULT 0,
    llm_cost_micro_usd  INTEGER NOT NULL DEFAULT 0
)
"""

# Columns added after the table first shipped (#51, then #93). CREATE TABLE IF
# NOT EXISTS is a no-op on a database created by an earlier version, so every
# store's setup() also has to add anything missing — otherwise upgrading a
# deployment with an existing sqlite file or postgres database breaks every query
# below. A row that predates a column reads as 0 for it, which is the same "not
# scored yet" the columns mean on a fresh row.
_ADDED_COLUMNS = (
    ("criteria_count", "INTEGER NOT NULL DEFAULT 0"),
    ("match_count", "INTEGER NOT NULL DEFAULT 0"),
    ("coverage_checkable", "INTEGER NOT NULL DEFAULT 0"),
    ("coverage_criteria", "INTEGER NOT NULL DEFAULT 0"),
    ("llm_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("llm_cost_micro_usd", "INTEGER NOT NULL DEFAULT 0"),
)

# Every list query orders by created_at DESC. Without this the table is only
# indexed on its thread_id primary key, so each page view is a full scan plus a
# sort — over rows that carry the whole protocol text, which makes the scan far
# more expensive than the 25 rows it returns. Same statement works on both
# engines.
_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_screenings_created_at ON screenings(created_at DESC)"
)

_LIST_COLUMNS = (
    "thread_id, source_filename, status, created_at, criteria_count, match_count, "
    "coverage_checkable, coverage_criteria, llm_tokens, llm_cost_micro_usd"
)


def _record(row: Sequence[Any]) -> ScreeningRecord:
    """One `_LIST_COLUMNS` row → a record. Shared by both SQL stores.

    Typed as a Sequence rather than a tuple so it accepts both drivers' row
    objects (aiosqlite's `Row`, psycopg's tuple) without a cast at each call.
    """
    return ScreeningRecord(
        row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]
    )


class SqliteScreeningStore(ScreeningStore):
    """aiosqlite-backed store. Its own connection (WAL) so it never contends
    with the checkpointer's transactions on the shared file."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TABLE)
        # sqlite has no ADD COLUMN IF NOT EXISTS, so ask what's already there.
        async with self._conn.execute("PRAGMA table_info(screenings)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for column, ddl in _ADDED_COLUMNS:
            if column not in existing:
                await self._conn.execute(f"ALTER TABLE screenings ADD COLUMN {column} {ddl}")
                log.info("persistence.column_added", column=column)
        await self._conn.execute(_CREATE_INDEX)
        await self._conn.commit()

    async def create(self, thread_id: str, source_filename: str, raw_protocol_text: str) -> None:
        await self._conn.execute(
            "INSERT INTO screenings "
            "(thread_id, source_filename, raw_protocol_text, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, source_filename, raw_protocol_text, "routing", _now()),
        )
        await self._conn.commit()

    async def exists(self, thread_id: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM screenings WHERE thread_id = ?", (thread_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def get_input(self, thread_id: str) -> ScreeningInput | None:
        async with self._conn.execute(
            "SELECT raw_protocol_text, source_filename FROM screenings WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ScreeningInput(raw_protocol_text=row[0], source_filename=row[1])

    async def get_record(self, thread_id: str) -> ScreeningRecord | None:
        async with self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM screenings WHERE thread_id = ?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
        return _record(row) if row else None

    async def set_status(
        self,
        thread_id: str,
        status: str,
        *,
        criteria_count: int | None = None,
        match_count: int | None = None,
        coverage_checkable: int | None = None,
        coverage_criteria: int | None = None,
        llm_tokens: int | None = None,
        llm_cost_micro_usd: int | None = None,
    ) -> None:
        # COALESCE keeps a NULL argument from clobbering a stored count, so this
        # stays a single statement for both "status only" and "status + counts".
        await self._conn.execute(
            "UPDATE screenings SET status = ?, "
            "criteria_count = COALESCE(?, criteria_count), "
            "match_count = COALESCE(?, match_count), "
            "coverage_checkable = COALESCE(?, coverage_checkable), "
            "coverage_criteria = COALESCE(?, coverage_criteria), "
            "llm_tokens = COALESCE(?, llm_tokens), "
            "llm_cost_micro_usd = COALESCE(?, llm_cost_micro_usd) "
            "WHERE thread_id = ?",
            (
                status,
                criteria_count,
                match_count,
                coverage_checkable,
                coverage_criteria,
                llm_tokens,
                llm_cost_micro_usd,
                thread_id,
            ),
        )
        await self._conn.commit()

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        search: str | None = None,
    ) -> ScreeningPage:
        where, params = _list_filters(status, search, "?", "LIKE")
        async with self._conn.execute(
            f"SELECT COUNT(*) FROM screenings{where}", tuple(params)
        ) as cur:
            total = (await cur.fetchone())[0]  # type: ignore[index]
        async with self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM screenings{where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        return ScreeningPage(items=[_record(r) for r in rows], total=total)


class PostgresScreeningStore(ScreeningStore):
    """psycopg-backed store for production. Same schema, ``%s`` placeholders."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TABLE)
        for column, ddl in _ADDED_COLUMNS:
            # Postgres has the IF NOT EXISTS form, so no introspection needed.
            await self._conn.execute(
                f"ALTER TABLE screenings ADD COLUMN IF NOT EXISTS {column} {ddl}"
            )
        await self._conn.execute(_CREATE_INDEX)
        await self._conn.commit()

    async def create(self, thread_id: str, source_filename: str, raw_protocol_text: str) -> None:
        await self._conn.execute(
            "INSERT INTO screenings "
            "(thread_id, source_filename, raw_protocol_text, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (thread_id, source_filename, raw_protocol_text, "routing", _now()),
        )
        await self._conn.commit()

    async def exists(self, thread_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM screenings WHERE thread_id = %s", (thread_id,)
        )
        return await cur.fetchone() is not None

    async def get_input(self, thread_id: str) -> ScreeningInput | None:
        cur = await self._conn.execute(
            "SELECT raw_protocol_text, source_filename FROM screenings WHERE thread_id = %s",
            (thread_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return ScreeningInput(raw_protocol_text=row[0], source_filename=row[1])

    async def get_record(self, thread_id: str) -> ScreeningRecord | None:
        cur = await self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM screenings WHERE thread_id = %s", (thread_id,)
        )
        row = await cur.fetchone()
        return _record(row) if row else None

    async def set_status(
        self,
        thread_id: str,
        status: str,
        *,
        criteria_count: int | None = None,
        match_count: int | None = None,
        coverage_checkable: int | None = None,
        coverage_criteria: int | None = None,
        llm_tokens: int | None = None,
        llm_cost_micro_usd: int | None = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE screenings SET status = %s, "
            "criteria_count = COALESCE(%s, criteria_count), "
            "match_count = COALESCE(%s, match_count), "
            "coverage_checkable = COALESCE(%s, coverage_checkable), "
            "coverage_criteria = COALESCE(%s, coverage_criteria), "
            "llm_tokens = COALESCE(%s, llm_tokens), "
            "llm_cost_micro_usd = COALESCE(%s, llm_cost_micro_usd) "
            "WHERE thread_id = %s",
            (
                status,
                criteria_count,
                match_count,
                coverage_checkable,
                coverage_criteria,
                llm_tokens,
                llm_cost_micro_usd,
                thread_id,
            ),
        )
        await self._conn.commit()

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        search: str | None = None,
    ) -> ScreeningPage:
        # ILIKE, not LIKE: postgres's LIKE is case-sensitive, sqlite's is not.
        where, params = _list_filters(status, search, "%s", "ILIKE")
        cur = await self._conn.execute(f"SELECT COUNT(*) FROM screenings{where}", tuple(params))
        row = await cur.fetchone()
        total = row[0] if row else 0
        cur = await self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM screenings{where} "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
        rows = await cur.fetchall()
        return ScreeningPage(items=[_record(r) for r in rows], total=total)


# --- The decision index (#98) -----------------------------------------------


@dataclass(frozen=True)
class AuditDecision:
    """One human decision, as it is handed to the index.

    Written at the moment the decision is made (see ``services/audit.record``),
    never derived by walking checkpoints at query time — which is what makes
    ``GET /api/audit`` a bounded read over one indexed table rather than a load of
    every run this instance has ever performed.

    PHI-safe by construction, and structurally so: these eight fields are the
    whole vocabulary, and none of them has anywhere for a patient to be. Staff
    identity, the action, when, which run, and a sentence about the *protocol*.
    ``services/audit.py`` documents the rule; ``tests/test_audit.py`` asserts it.

    ``revision`` is the criteria revision a ``criteria_revised`` entry produced
    and 0 for every other action — it is what lets an entry link to the specific
    before/after diff in the run's checkpoint rather than to the run at large.
    ``source_filename`` is denormalized from the screening row so the index reads
    without a join: an auditor's first scan is by protocol name, and the row it
    would join against can be deleted while the decision must not be.
    """

    thread_id: str
    action: str
    actor: str
    actor_role: str
    occurred_at: str
    detail: str
    revision: int = 0
    source_filename: str = ""


@dataclass(frozen=True)
class AuditRecord(AuditDecision):
    """A stored decision, plus the sequence number the index gave it.

    ``id`` is assigned by the database, so it is monotonic in insertion order and
    is what breaks a tie between two decisions sharing a timestamp — two reviewers
    acting in the same millisecond must still have a total order, or a page
    boundary can drop or repeat a row.
    """

    id: int = 0


@dataclass(frozen=True)
class AuditPage:
    """One page of ``AuditStore.list`` results plus the total the filter matched."""

    items: list[AuditRecord]
    total: int


@dataclass(frozen=True)
class AuditFilter:
    """The narrowing an audit query asks for. Every field is optional and ANDs.

    ``since``/``until`` are inclusive bounds compared against ``occurred_at`` as
    *strings*. That is sound rather than a shortcut: every stamp is written by
    ``datetime.now(UTC).isoformat()``, so they share one format and one offset,
    and ISO-8601 in a fixed format sorts lexicographically the way it sorts
    chronologically. ``services/audit.parse_bound`` is what normalizes a caller's
    date into that format, and the one place that rule is applied.
    """

    actor: str | None = None
    action: str | None = None
    thread_id: str | None = None
    since: str | None = None
    until: str | None = None


class AuditStore(ABC):
    """Thin async repository for the org-wide decision index. No ORM."""

    @abstractmethod
    async def setup(self) -> None:
        """Create the table and its indexes if absent. Idempotent."""

    @abstractmethod
    async def record(self, decision: AuditDecision) -> None:
        """Append one decision. Never updates, never deletes — this is a ledger."""

    @abstractmethod
    async def list(
        self, *, limit: int, offset: int, filters: AuditFilter | None = None
    ) -> AuditPage:
        """One page of decisions, newest first, plus the total matching the filter."""


def _audit_filters(filters: AuditFilter, placeholder: str) -> tuple[str, list[str]]:
    """The shared WHERE clause for ``AuditStore.list`` and its COUNT twin.

    Exact matches on actor/action/thread_id and a closed range on ``occurred_at``.
    No LIKE anywhere: every one of these is an identifier a caller either has or
    does not, and a substring match on an actor would let ``ana@`` return
    ``susana@``'s decisions to someone scoped away from them.
    """
    clauses: list[str] = []
    params: list[str] = []
    for column, value in (
        ("actor", filters.actor),
        ("action", filters.action),
        ("thread_id", filters.thread_id),
    ):
        if value:
            clauses.append(f"{column} = {placeholder}")
            params.append(value)
    if filters.since:
        clauses.append(f"occurred_at >= {placeholder}")
        params.append(filters.since)
    if filters.until:
        clauses.append(f"occurred_at <= {placeholder}")
        params.append(filters.until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# Newest first, with the sequence number as the tiebreaker — see `AuditRecord.id`.
_AUDIT_ORDER = " ORDER BY occurred_at DESC, id DESC"

_AUDIT_COLUMNS = (
    "id, thread_id, action, actor, actor_role, occurred_at, detail, revision, source_filename"
)

_INSERT_AUDIT_COLUMNS = (
    "thread_id, action, actor, actor_role, occurred_at, detail, revision, source_filename"
)

# The `id` column is the one piece of DDL the two engines spell differently.
_CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS audit_events (
    id              {pk},
    thread_id       TEXT NOT NULL,
    action          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    actor_role      TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    detail          TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 0,
    source_filename TEXT NOT NULL DEFAULT ''
)
"""

# One index per way the page is asked for. The first is the default ordering and
# every unfiltered page view; the other two are the filters an auditor actually
# narrows by — a person, or a run — and without them each is a full scan of a
# table that only ever grows. Both statements work on either engine.
_CREATE_AUDIT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at "
    "ON audit_events(occurred_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_actor ON audit_events(actor, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_thread ON audit_events(thread_id, occurred_at)",
)


def _audit_record(row: Sequence[Any]) -> AuditRecord:
    """One `_AUDIT_COLUMNS` row → a record. Shared by both SQL stores."""
    return AuditRecord(
        id=row[0],
        thread_id=row[1],
        action=row[2],
        actor=row[3],
        actor_role=row[4],
        occurred_at=row[5],
        detail=row[6],
        revision=row[7],
        source_filename=row[8],
    )


def _audit_values(decision: AuditDecision) -> tuple[Any, ...]:
    """The insert parameters for one decision, in `_INSERT_AUDIT_COLUMNS` order."""
    return (
        decision.thread_id,
        decision.action,
        decision.actor,
        decision.actor_role,
        decision.occurred_at,
        decision.detail,
        decision.revision,
        decision.source_filename,
    )


class InMemoryAuditStore(AuditStore):
    """List-backed index for tests — no durability, no I/O, no event loop needed."""

    def __init__(self) -> None:
        self._rows: list[AuditRecord] = []

    async def setup(self) -> None:
        return None

    async def record(self, decision: AuditDecision) -> None:
        # `len + 1` reproduces the SQL stores' 1-based autoincrement, so a test
        # asserting on ids reads the same on either backend.
        self._rows.append(AuditRecord(**vars(decision), id=len(self._rows) + 1))

    async def list(
        self, *, limit: int, offset: int, filters: AuditFilter | None = None
    ) -> AuditPage:
        criteria = filters or AuditFilter()
        rows = [row for row in self._rows if _matches(row, criteria)]
        # Mirrors `_AUDIT_ORDER`: newest first, sequence number breaking the tie.
        rows.sort(key=lambda row: (row.occurred_at, row.id), reverse=True)
        return AuditPage(items=rows[offset : offset + limit], total=len(rows))


def _matches(row: AuditRecord, filters: AuditFilter) -> bool:
    """The in-memory twin of `_audit_filters` — the same ANDed exact matches."""
    return not (
        (filters.actor and row.actor != filters.actor)
        or (filters.action and row.action != filters.action)
        or (filters.thread_id and row.thread_id != filters.thread_id)
        or (filters.since and row.occurred_at < filters.since)
        or (filters.until and row.occurred_at > filters.until)
    )


class SqliteAuditStore(AuditStore):
    """aiosqlite-backed index, sharing the store's connection.

    Sharing is safe and deliberate: every statement here is a single autocommit
    INSERT or SELECT, exactly like ``SqliteScreeningStore``'s, so there is no
    multi-statement transaction for the two to interleave inside (see
    ``_open_sqlite`` for why that connection is in autocommit mode at all). A
    third connection to the same file would only add another writer to contend
    for the WAL's single write lock.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_AUDIT_TABLE.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT"))
        for statement in _CREATE_AUDIT_INDEXES:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def record(self, decision: AuditDecision) -> None:
        await self._conn.execute(
            f"INSERT INTO audit_events ({_INSERT_AUDIT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            _audit_values(decision),
        )
        await self._conn.commit()

    async def list(
        self, *, limit: int, offset: int, filters: AuditFilter | None = None
    ) -> AuditPage:
        where, params = _audit_filters(filters or AuditFilter(), "?")
        async with self._conn.execute(
            f"SELECT COUNT(*) FROM audit_events{where}", tuple(params)
        ) as cur:
            total = (await cur.fetchone())[0]  # type: ignore[index]
        async with self._conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_events{where}{_AUDIT_ORDER} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        return AuditPage(items=[_audit_record(row) for row in rows], total=total)


class PostgresAuditStore(AuditStore):
    """psycopg-backed index for production. Same schema, ``%s`` placeholders."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_AUDIT_TABLE.format(pk="BIGSERIAL PRIMARY KEY"))
        for statement in _CREATE_AUDIT_INDEXES:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def record(self, decision: AuditDecision) -> None:
        await self._conn.execute(
            f"INSERT INTO audit_events ({_INSERT_AUDIT_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            _audit_values(decision),
        )
        await self._conn.commit()

    async def list(
        self, *, limit: int, offset: int, filters: AuditFilter | None = None
    ) -> AuditPage:
        where, params = _audit_filters(filters or AuditFilter(), "%s")
        cur = await self._conn.execute(f"SELECT COUNT(*) FROM audit_events{where}", tuple(params))
        row = await cur.fetchone()
        total = row[0] if row else 0
        cur = await self._conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_events{where}{_AUDIT_ORDER} LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
        rows = await cur.fetchall()
        return AuditPage(items=[_audit_record(r) for r in rows], total=total)


# --- Lifecycle --------------------------------------------------------------


@dataclass
class Persistence:
    """Bundles the checkpointer and both stores with their lifecycle.

    Built once per process in the app lifespan; ``aclose`` releases every
    connection on shutdown.
    """

    backend: str
    checkpointer: BaseCheckpointSaver
    store: ScreeningStore
    audit: AuditStore
    _closers: list[Callable[[], Awaitable[None]]]

    async def aclose(self) -> None:
        for close in self._closers:
            await close()


async def open_persistence(settings: Settings) -> Persistence:
    """Open connections, create tables, and wire up checkpointer + store."""
    backend = settings.checkpoint_backend
    log.info("persistence.opening", backend=backend)

    if backend == "memory":
        checkpointer: BaseCheckpointSaver = MemorySaver()
        store: ScreeningStore = InMemoryScreeningStore()
        audit: AuditStore = InMemoryAuditStore()
        await store.setup()
        await audit.setup()
        return Persistence(backend, checkpointer, store, audit, [])

    if backend == "sqlite":
        return await _open_sqlite(settings)

    if backend == "postgres":
        return await _open_postgres(settings)

    raise ValueError(f"Unknown checkpoint backend: {backend}")  # pragma: no cover


async def _open_sqlite(settings: Settings) -> Persistence:
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = str(settings.sqlite_path)

    # Separate connections for the checkpointer and the store so their
    # transactions never step on each other. WAL (persisted in the file header,
    # so it must be set on the first connection *before* the second opens, or
    # the switch deadlocks) lets readers and a single writer run concurrently —
    # and lets a second uvicorn worker share the same file without split-brain.
    saver_conn = await aiosqlite.connect(path)
    await saver_conn.execute("PRAGMA journal_mode=WAL")
    # isolation_level=None puts the store in autocommit mode. This is
    # load-bearing, not a style choice (#10): with Python's *default* implicit
    # transactions, the shared store connection fast-failed writes with "database
    # is locked" under concurrent load (measured: ~76% of creates failed at
    # 50 users), because a write that has to promote an already-open implicit
    # transaction takes an *immediate* SQLITE_BUSY that busy_timeout does not
    # cover. Autocommit issues each INSERT/UPDATE as a standalone statement that
    # acquires the write lock directly — the path where busy_timeout IS honored,
    # so a contended writer waits out the lock instead of erroring (measured: the
    # same load dropped to <0.5% errors). The store only ever does
    # single-statement writes, so it needs no multi-statement transactions.
    # Full analysis and numbers: docs/performance.md.
    store_conn = await aiosqlite.connect(path, isolation_level=None)
    for conn in (saver_conn, store_conn):
        # Wait out a briefly-held write lock instead of erroring (honored on the
        # direct write-lock path; see the autocommit note above).
        await conn.execute("PRAGMA busy_timeout=5000")

    checkpointer = AsyncSqliteSaver(saver_conn)
    await checkpointer.setup()
    store = SqliteScreeningStore(store_conn)
    await store.setup()
    # Shares `store_conn` — see SqliteAuditStore for why that is the right number
    # of writers rather than one too few.
    audit = SqliteAuditStore(store_conn)
    await audit.setup()

    return Persistence("sqlite", checkpointer, store, audit, [saver_conn.close, store_conn.close])


async def _open_postgres(settings: Settings) -> Persistence:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection

    dsn = settings.postgres_dsn
    assert dsn is not None  # guaranteed by Settings validation

    # Both autocommit: the saver manages its own transactions, and the store's
    # statements (including SELECTs) must not linger as idle-open transactions.
    saver_conn = await AsyncConnection.connect(dsn, autocommit=True)
    store_conn = await AsyncConnection.connect(dsn, autocommit=True)

    checkpointer = AsyncPostgresSaver(saver_conn)
    await checkpointer.setup()
    store = PostgresScreeningStore(store_conn)
    await store.setup()
    audit = PostgresAuditStore(store_conn)
    await audit.setup()

    return Persistence("postgres", checkpointer, store, audit, [saver_conn.close, store_conn.close])
