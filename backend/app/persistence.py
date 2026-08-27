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
4. **The compliance rules** — the Critic's deterministic layer, authored by
   admins rather than baked into the image (#97). ``app/rules/compliance_rules.yaml``
   seeds the table on first boot and stays in the repo as the documented default
   set; from then on this table is what the engine runs. A rule is retired by
   flipping ``enabled``, never by deleting the row: a finding cites a rule id
   forever, and a deleted rule would leave every past finding unresolvable.

All three live in the *same* database, selected by ``CHECKPOINT_BACKEND``:

- ``memory``   — process-local, lost on restart (tests only).
- ``sqlite``   — durable single-node default.
- ``postgres`` — multi-replica production target (deps in the ``postgres`` extra).

Route handlers never touch SQL: they call the ``ScreeningStore`` /
``AuditStore`` / ``RuleStore`` repositories. Nothing here is constructed at
import — ``AsyncSqliteSaver`` captures the running loop in its constructor, so
everything is built inside ``open_persistence`` from the app's lifespan.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
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
    # When the run last entered a human stop (#103). Denormalized so gate age can
    # render on the runs index without loading checkpoints. Null for runs that
    # never parked, or rows written before this column shipped.
    gate_entered_at: str | None = None
    # When a stale reminder last left the process for this run. Persisted so a
    # redeployed instance does not re-notify runs it already chased.
    last_reminder_at: str | None = None


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

    @abstractmethod
    async def list_parked(self) -> builtins.list[ScreeningRecord]:
        """Every run waiting on a human — ``awaiting_approval`` or ``escalated``."""

    @abstractmethod
    async def mark_gate_entered(self, thread_id: str, entered_at: str) -> None:
        """Record (or refresh) when a run entered a human stop (#103).

        Clears ``last_reminder_at`` so a re-parked run starts a fresh reminder
        schedule rather than inheriting the previous wait's cadence.
        """

    @abstractmethod
    async def mark_reminder_sent(self, thread_id: str, sent_at: str) -> None:
        """Persist that a stale reminder was dispatched for this run."""

    @abstractmethod
    async def get_meta(self, key: str) -> str | None: ...

    @abstractmethod
    async def set_meta(self, key: str, value: str) -> None: ...


class InMemoryScreeningStore(ScreeningStore):
    """Dict-backed store for tests — no durability, no I/O, no event loop needed."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._meta: dict[str, str] = {}

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
            "gate_entered_at": None,
            "last_reminder_at": None,
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
            row.get("gate_entered_at"),
            row.get("last_reminder_at"),
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

    async def list_parked(self) -> builtins.list[ScreeningRecord]:
        rows = [r for r in self._rows.values() if r["status"] in ("awaiting_approval", "escalated")]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._as_record(r) for r in rows]

    async def mark_gate_entered(self, thread_id: str, entered_at: str) -> None:
        row = self._rows.get(thread_id)
        if row is not None:
            row["gate_entered_at"] = entered_at
            row["last_reminder_at"] = None

    async def mark_reminder_sent(self, thread_id: str, sent_at: str) -> None:
        row = self._rows.get(thread_id)
        if row is not None:
            row["last_reminder_at"] = sent_at

    async def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    async def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value


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
    llm_cost_micro_usd  INTEGER NOT NULL DEFAULT 0,
    gate_entered_at     TEXT,
    last_reminder_at    TEXT
)
"""

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Columns added after the table first shipped (#51, then #93, then #103). CREATE TABLE IF
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
    ("gate_entered_at", "TEXT"),
    ("last_reminder_at", "TEXT"),
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
    "coverage_checkable, coverage_criteria, llm_tokens, llm_cost_micro_usd, "
    "gate_entered_at, last_reminder_at"
)


def _record(row: Sequence[Any]) -> ScreeningRecord:
    """One `_LIST_COLUMNS` row → a record. Shared by both SQL stores.

    Typed as a Sequence rather than a tuple so it accepts both drivers' row
    objects (aiosqlite's `Row`, psycopg's tuple) without a cast at each call.
    """
    return ScreeningRecord(
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
        row[7],
        row[8],
        row[9],
        row[10] if len(row) > 10 else None,
        row[11] if len(row) > 11 else None,
    )


class SqliteScreeningStore(ScreeningStore):
    """aiosqlite-backed store. Its own connection (WAL) so it never contends
    with the checkpointer's transactions on the shared file."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TABLE)
        await self._conn.execute(_CREATE_META_TABLE)
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

    async def list_parked(self) -> builtins.list[ScreeningRecord]:
        async with self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM screenings "
            "WHERE status IN ('awaiting_approval', 'escalated') "
            "ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [_record(r) for r in rows]

    async def mark_gate_entered(self, thread_id: str, entered_at: str) -> None:
        await self._conn.execute(
            "UPDATE screenings SET gate_entered_at = ?, last_reminder_at = NULL "
            "WHERE thread_id = ?",
            (entered_at, thread_id),
        )
        await self._conn.commit()

    async def mark_reminder_sent(self, thread_id: str, sent_at: str) -> None:
        await self._conn.execute(
            "UPDATE screenings SET last_reminder_at = ? WHERE thread_id = ?",
            (sent_at, thread_id),
        )
        await self._conn.commit()

    async def get_meta(self, key: str) -> str | None:
        async with self._conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()


class PostgresScreeningStore(ScreeningStore):
    """psycopg-backed store for production. Same schema, ``%s`` placeholders."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TABLE)
        await self._conn.execute(_CREATE_META_TABLE)
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

    async def list_parked(self) -> builtins.list[ScreeningRecord]:
        cur = await self._conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM screenings "
            "WHERE status IN ('awaiting_approval', 'escalated') "
            "ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [_record(r) for r in rows]

    async def mark_gate_entered(self, thread_id: str, entered_at: str) -> None:
        await self._conn.execute(
            "UPDATE screenings SET gate_entered_at = %s, last_reminder_at = NULL "
            "WHERE thread_id = %s",
            (entered_at, thread_id),
        )
        await self._conn.commit()

    async def mark_reminder_sent(self, thread_id: str, sent_at: str) -> None:
        await self._conn.execute(
            "UPDATE screenings SET last_reminder_at = %s WHERE thread_id = %s",
            (sent_at, thread_id),
        )
        await self._conn.commit()

    async def get_meta(self, key: str) -> str | None:
        cur = await self._conn.execute("SELECT value FROM app_meta WHERE key = %s", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
        await self._conn.commit()


# --- The decision index (#98) -----------------------------------------------

# What an audit entry is about. A run for every decision the index carried at
# first (#98); a compliance rule for the authoring actions (#97). Closed, and
# defaulted to the run so a row written before the column existed reads as what
# it actually was rather than as an unknown kind.
SUBJECT_SCREENING = "screening"
SUBJECT_RULE = "rule"


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

    ``subject_kind``/``subject_id`` are what the entry is *about* (#97). Every
    decision this index carried at first was about a run, so ``thread_id`` was
    the whole answer; authoring a compliance rule is a decision about no run at
    all. Rather than overload ``thread_id`` — a column named for one thing,
    quietly holding another, that every existing filter and link already trusts —
    the subject is named outright. ``thread_id`` stays as the run filter the API
    exposes, and ``subject_id`` mirrors it for a screening entry so one column
    answers "what was this about" for every row.
    """

    thread_id: str
    action: str
    actor: str
    actor_role: str
    occurred_at: str
    detail: str
    revision: int = 0
    source_filename: str = ""
    subject_kind: str = SUBJECT_SCREENING
    subject_id: str = ""


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
    "id, thread_id, action, actor, actor_role, occurred_at, detail, revision, source_filename, "
    "subject_kind, subject_id"
)

_INSERT_AUDIT_COLUMNS = (
    "thread_id, action, actor, actor_role, occurred_at, detail, revision, source_filename, "
    "subject_kind, subject_id"
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
    source_filename TEXT NOT NULL DEFAULT '',
    subject_kind    TEXT NOT NULL DEFAULT 'screening',
    subject_id      TEXT NOT NULL DEFAULT ''
)
"""

# Added after the index first shipped (#98 → #97), the same way `_ADDED_COLUMNS`
# extends the screenings table. The defaults are what make the migration total:
# every existing row is a decision about a run, which is exactly what
# `'screening'` says.
_ADDED_AUDIT_COLUMNS = (
    ("subject_kind", "TEXT NOT NULL DEFAULT 'screening'"),
    ("subject_id", "TEXT NOT NULL DEFAULT ''"),
)

# Backfill for rows written before `subject_id` existed: their subject is the run
# they name. Runs once in effect — after it, no row matches the WHERE — so it is
# safe to re-issue on every startup, which is what makes it idempotent without a
# migration-version table. Scoped to `screening` rows so it can never touch a
# rule entry whose `thread_id` is deliberately empty.
_BACKFILL_AUDIT_SUBJECT = (
    "UPDATE audit_events SET subject_id = thread_id "
    f"WHERE subject_id = '' AND subject_kind = '{SUBJECT_SCREENING}' AND thread_id <> ''"
)

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
        subject_kind=row[9],
        subject_id=row[10],
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
        decision.subject_kind,
        # Defaulted from `thread_id` rather than required at every call site: a
        # decision about a run already names it, and making the two disagree
        # should not be something a caller can do by forgetting an argument.
        decision.subject_id or decision.thread_id,
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
        # A *copy* of the fields: `vars()` hands back the instance's own __dict__,
        # and assigning into it would reach through the frozen dataclass and edit
        # the caller's decision — a store that silently rewrites what it was
        # handed is the last place that should happen.
        fields = {**vars(decision)}
        # The same default `_audit_values` applies, applied here too rather than
        # only in SQL — a store that disagreed with its twin about what was
        # written would make the in-memory backend useless for testing the rest.
        fields["subject_id"] = decision.subject_id or decision.thread_id
        self._rows.append(AuditRecord(**fields, id=len(self._rows) + 1))

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
        # sqlite has no ADD COLUMN IF NOT EXISTS — same introspection the
        # screenings table needs, for the same upgrade-in-place reason.
        async with self._conn.execute("PRAGMA table_info(audit_events)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for column, ddl in _ADDED_AUDIT_COLUMNS:
            if column not in existing:
                await self._conn.execute(f"ALTER TABLE audit_events ADD COLUMN {column} {ddl}")
                log.info("persistence.column_added", table="audit_events", column=column)
        await self._conn.execute(_BACKFILL_AUDIT_SUBJECT)
        for statement in _CREATE_AUDIT_INDEXES:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def record(self, decision: AuditDecision) -> None:
        await self._conn.execute(
            f"INSERT INTO audit_events ({_INSERT_AUDIT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        for column, ddl in _ADDED_AUDIT_COLUMNS:
            await self._conn.execute(
                f"ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS {column} {ddl}"
            )
        await self._conn.execute(_BACKFILL_AUDIT_SUBJECT)
        for statement in _CREATE_AUDIT_INDEXES:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def record(self, decision: AuditDecision) -> None:
        await self._conn.execute(
            f"INSERT INTO audit_events ({_INSERT_AUDIT_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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


# --- The rules table (#97) --------------------------------------------------


@dataclass(frozen=True)
class RuleRecord:
    """One authored rule of the Critic's deterministic layer.

    The stored shape of what ``app/rules/compliance_rules.yaml`` holds, plus the
    four attribution columns and the ``enabled`` flag the file has no room for.
    The contract a rule keeps is the one the engine already reads (``id``,
    ``check``, ``description``, optional ``plain``, plus whatever the check kind
    requires) — see ``services/rules.py`` for the validation that enforces it.

    **``params`` rather than a column per threshold.** ``range`` needs two bounds,
    ``keyword_implies_criterion`` needs a category, and the other two kinds need
    neither. A column each would be four mostly-NULL columns whose meaning depends
    on another column's value, and every new check kind would be a migration. The
    check kind decides what belongs in here and ``services/rules.validate`` is the
    one place that knows the mapping.

    **``position`` keeps file order.** The seed file groups rules by clinical
    domain and comments each group, which is the order the person maintaining them
    thinks in; sorting by id would scatter it. New rules append.

    ``enabled`` is the soft retirement the issue asks for: a disabled rule stops
    firing but keeps its row, so a finding that cites it still resolves and the
    viewer can render it as retired rather than as a broken link.
    """

    id: str
    check: str
    description: str
    position: int = 0
    attribute: str = ""
    plain: str = ""
    keywords: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created_by: str = ""
    created_at: str = ""
    updated_by: str = ""
    updated_at: str = ""

    def as_engine_rule(self) -> dict[str, Any]:
        """The row in the shape ``run_deterministic_checks`` reads.

        Flattening ``params`` back to top-level keys is what lets the engine, the
        seed file and the API all speak one vocabulary: the YAML the repo ships
        and the dict the engine iterates are the same document, so a rule authored
        through the API and one read from the file are indistinguishable by the
        time they reach the check.
        """
        return {
            "id": self.id,
            "attribute": self.attribute,
            "check": self.check,
            "description": self.description,
            "plain": self.plain,
            "keywords": list(self.keywords),
            **self.params,
        }


class RuleStore(ABC):
    """Thin async repository for the authored rules. No ORM."""

    @abstractmethod
    async def setup(self) -> None:
        """Create the table if absent. Idempotent."""

    @abstractmethod
    async def seed(self, records: Sequence[RuleRecord]) -> int:
        """Insert `records` only if the table is empty; return how many landed.

        The whole of "the YAML seeds the table on first boot, and the DB is the
        source of truth thereafter": once a single row exists this is a no-op, so
        an admin's edits are never silently reverted by a redeploy.

        The emptiness check and the inserts are not one transaction, and on the
        multi-replica target they are not even one process: replicas boot
        together, so two can both find the table empty and both start inserting.
        Every implementation therefore ignores a row whose id is already present,
        which makes a lost race a no-op rather than an IntegrityError that takes
        down the second replica's startup. The return value is what was
        *attempted*, not what won the race — it feeds a log line, not a decision.
        """

    @abstractmethod
    async def list(self, *, include_disabled: bool = True) -> list[RuleRecord]:
        """Every rule in `position` order — the engine asks for enabled only."""

    @abstractmethod
    async def get(self, rule_id: str) -> RuleRecord | None:
        """One rule by id, enabled or not."""

    @abstractmethod
    async def add(self, record: RuleRecord) -> None:
        """Insert one authored rule. The caller has already checked the id is free."""

    @abstractmethod
    async def replace(self, record: RuleRecord) -> None:
        """Overwrite one rule's authored fields, keeping its id and creation stamps."""

    @abstractmethod
    async def set_enabled(self, rule_id: str, enabled: bool, actor: str, at: str) -> None:
        """Retire or restore a rule, recording who and when."""

    @abstractmethod
    async def next_position(self) -> int:
        """Where a newly authored rule sorts: after everything already there."""


_CREATE_RULES_TABLE = """
CREATE TABLE IF NOT EXISTS compliance_rules (
    id          TEXT PRIMARY KEY,
    position    INTEGER NOT NULL DEFAULT 0,
    attribute   TEXT NOT NULL DEFAULT '',
    check_kind  TEXT NOT NULL,
    description TEXT NOT NULL,
    plain       TEXT NOT NULL DEFAULT '',
    keywords    TEXT NOT NULL DEFAULT '[]',
    params      TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
)
"""

# `check` is a reserved word in SQL (the column constraint), so the column is
# `check_kind` while every other layer — the YAML, the engine, the API payload —
# says `check`. The translation lives here and nowhere else.
_RULE_COLUMNS = (
    "id, position, attribute, check_kind, description, plain, keywords, params, enabled, "
    "created_by, created_at, updated_by, updated_at"
)

_RULE_ORDER = " ORDER BY position, id"


def _rule_record(row: Sequence[Any]) -> RuleRecord:
    """One `_RULE_COLUMNS` row → a record. Shared by both SQL stores.

    `keywords`/`params` are stored as JSON text rather than in a JSON column type:
    sqlite has none, and the two engines' JSON types differ enough that one
    encoding in the application is simpler than two in the schema. A row whose
    JSON is unreadable degrades to empty rather than raising — the same tolerance
    `services/rules._text` applies to the file, and for the same reason: an
    operator who hand-edited the database must still be able to load the page that
    shows them what they broke.
    """
    return RuleRecord(
        id=row[0],
        position=row[1],
        attribute=row[2],
        check=row[3],
        description=row[4],
        plain=row[5],
        keywords=_json_list(row[6]),
        params=_json_dict(row[7]),
        enabled=bool(row[8]),
        created_by=row[9],
        created_at=row[10],
        updated_by=row[11],
        updated_at=row[12],
    )


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _json_dict(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _rule_values(record: RuleRecord) -> tuple[Any, ...]:
    """The insert parameters for one rule, in `_RULE_COLUMNS` order."""
    return (
        record.id,
        record.position,
        record.attribute,
        record.check,
        record.description,
        record.plain,
        json.dumps(record.keywords),
        json.dumps(record.params),
        int(record.enabled),
        record.created_by,
        record.created_at,
        record.updated_by,
        record.updated_at,
    )


# The authored fields an update overwrites. Deliberately excludes `id`,
# `position`, `created_by` and `created_at`: an edit revises a rule, it does not
# re-create one, and rewriting who first authored it would be a lie the audit log
# could not catch.
_RULE_UPDATE_SET = (
    "attribute = {p}, check_kind = {p}, description = {p}, plain = {p}, keywords = {p}, "
    "params = {p}, enabled = {p}, updated_by = {p}, updated_at = {p}"
)


def _rule_update_values(record: RuleRecord) -> tuple[Any, ...]:
    """Parameters for `_RULE_UPDATE_SET`, then the id for the WHERE."""
    return (
        record.attribute,
        record.check,
        record.description,
        record.plain,
        json.dumps(record.keywords),
        json.dumps(record.params),
        int(record.enabled),
        record.updated_by,
        record.updated_at,
        record.id,
    )


class InMemoryRuleStore(RuleStore):
    """Dict-backed rules for tests — no durability, no I/O."""

    def __init__(self) -> None:
        self._rows: dict[str, RuleRecord] = {}

    async def setup(self) -> None:
        return None

    async def seed(self, records: Sequence[RuleRecord]) -> int:
        if self._rows:
            return 0
        # `setdefault`, mirroring the SQL stores' ignore-on-conflict: a duplicate
        # id within one seed set keeps the first, rather than the last silently
        # winning.
        for record in records:
            self._rows.setdefault(record.id, record)
        return len(records)

    async def list(self, *, include_disabled: bool = True) -> list[RuleRecord]:
        rows = [r for r in self._rows.values() if include_disabled or r.enabled]
        return sorted(rows, key=lambda r: (r.position, r.id))

    async def get(self, rule_id: str) -> RuleRecord | None:
        return self._rows.get(rule_id)

    async def add(self, record: RuleRecord) -> None:
        self._rows[record.id] = record

    async def replace(self, record: RuleRecord) -> None:
        existing = self._rows[record.id]
        # Mirrors `_RULE_UPDATE_SET`: the immutable four survive the write.
        self._rows[record.id] = RuleRecord(
            **{
                **vars(record),
                "position": existing.position,
                "created_by": existing.created_by,
                "created_at": existing.created_at,
            }
        )

    async def set_enabled(self, rule_id: str, enabled: bool, actor: str, at: str) -> None:
        existing = self._rows[rule_id]
        self._rows[rule_id] = RuleRecord(
            **{**vars(existing), "enabled": enabled, "updated_by": actor, "updated_at": at}
        )

    async def next_position(self) -> int:
        return max((r.position for r in self._rows.values()), default=-1) + 1


class SqliteRuleStore(RuleStore):
    """aiosqlite-backed rules, sharing the store's autocommit connection."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_RULES_TABLE)
        await self._conn.commit()

    async def seed(self, records: Sequence[RuleRecord]) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM compliance_rules") as cur:
            row = await cur.fetchone()
        if row and row[0]:
            return 0
        for record in records:
            # OR IGNORE: another replica may have seeded between the count above
            # and this insert — see `RuleStore.seed`.
            await self._conn.execute(
                f"INSERT OR IGNORE INTO compliance_rules ({_RULE_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _rule_values(record),
            )
        await self._conn.commit()
        return len(records)

    async def list(self, *, include_disabled: bool = True) -> list[RuleRecord]:
        where = "" if include_disabled else " WHERE enabled = 1"
        async with self._conn.execute(
            f"SELECT {_RULE_COLUMNS} FROM compliance_rules{where}{_RULE_ORDER}"
        ) as cur:
            rows = await cur.fetchall()
        return [_rule_record(row) for row in rows]

    async def get(self, rule_id: str) -> RuleRecord | None:
        async with self._conn.execute(
            f"SELECT {_RULE_COLUMNS} FROM compliance_rules WHERE id = ?", (rule_id,)
        ) as cur:
            row = await cur.fetchone()
        return _rule_record(row) if row else None

    async def add(self, record: RuleRecord) -> None:
        await self._conn.execute(
            f"INSERT INTO compliance_rules ({_RULE_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _rule_values(record),
        )
        await self._conn.commit()

    async def replace(self, record: RuleRecord) -> None:
        await self._conn.execute(
            f"UPDATE compliance_rules SET {_RULE_UPDATE_SET.format(p='?')} WHERE id = ?",
            _rule_update_values(record),
        )
        await self._conn.commit()

    async def set_enabled(self, rule_id: str, enabled: bool, actor: str, at: str) -> None:
        await self._conn.execute(
            "UPDATE compliance_rules SET enabled = ?, updated_by = ?, updated_at = ? WHERE id = ?",
            (int(enabled), actor, at, rule_id),
        )
        await self._conn.commit()

    async def next_position(self) -> int:
        async with self._conn.execute("SELECT MAX(position) FROM compliance_rules") as cur:
            row = await cur.fetchone()
        return (row[0] + 1) if row and row[0] is not None else 0


class PostgresRuleStore(RuleStore):
    """psycopg-backed rules for production. Same schema, ``%s`` placeholders."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_RULES_TABLE)
        await self._conn.commit()

    async def seed(self, records: Sequence[RuleRecord]) -> int:
        cur = await self._conn.execute("SELECT COUNT(*) FROM compliance_rules")
        row = await cur.fetchone()
        if row and row[0]:
            return 0
        for record in records:
            # ON CONFLICT DO NOTHING is postgres's spelling of the sqlite store's
            # OR IGNORE — the replicas-boot-together race, see `RuleStore.seed`.
            await self._conn.execute(
                f"INSERT INTO compliance_rules ({_RULE_COLUMNS}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                _rule_values(record),
            )
        await self._conn.commit()
        return len(records)

    async def list(self, *, include_disabled: bool = True) -> list[RuleRecord]:
        where = "" if include_disabled else " WHERE enabled = 1"
        cur = await self._conn.execute(
            f"SELECT {_RULE_COLUMNS} FROM compliance_rules{where}{_RULE_ORDER}"
        )
        return [_rule_record(row) for row in await cur.fetchall()]

    async def get(self, rule_id: str) -> RuleRecord | None:
        cur = await self._conn.execute(
            f"SELECT {_RULE_COLUMNS} FROM compliance_rules WHERE id = %s", (rule_id,)
        )
        row = await cur.fetchone()
        return _rule_record(row) if row else None

    async def add(self, record: RuleRecord) -> None:
        await self._conn.execute(
            f"INSERT INTO compliance_rules ({_RULE_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            _rule_values(record),
        )
        await self._conn.commit()

    async def replace(self, record: RuleRecord) -> None:
        await self._conn.execute(
            f"UPDATE compliance_rules SET {_RULE_UPDATE_SET.format(p='%s')} WHERE id = %s",
            _rule_update_values(record),
        )
        await self._conn.commit()

    async def set_enabled(self, rule_id: str, enabled: bool, actor: str, at: str) -> None:
        await self._conn.execute(
            "UPDATE compliance_rules SET enabled = %s, updated_by = %s, updated_at = %s "
            "WHERE id = %s",
            # `int`, not the bool: the column is INTEGER on both engines (sqlite
            # has no boolean), and psycopg would otherwise adapt a Python bool to
            # postgres `boolean` and fail the assignment.
            (int(enabled), actor, at, rule_id),
        )
        await self._conn.commit()

    async def next_position(self) -> int:
        cur = await self._conn.execute("SELECT MAX(position) FROM compliance_rules")
        row = await cur.fetchone()
        return (row[0] + 1) if row and row[0] is not None else 0


# --- Cross-run term mapping cache (#105) ------------------------------------


@dataclass(frozen=True)
class TermRecord:
    """One persisted clinical terminology mapping verdict (#105).

    Keyed by `(criterion_value, patient_term, model_id)` where criterion_value
    and patient_term are normalized (stripped and lowercased). Model_id ensures
    that a model swap does not silently reuse another model's clinical judgement.

    `verdict` holds 'match', 'no_match', or 'uncertain'. 'uncertain' verdicts
    are cached too so ambiguous pairs are not re-queried from the LLM on every run.
    `created_at` provides a timestamp for cache auditability and invalidation.
    """

    criterion_value: str
    patient_term: str
    model_id: str
    verdict: str
    created_at: str


class TermStore(ABC):
    """Thin async repository for durable cross-run term mappings (#105). No ORM."""

    @abstractmethod
    async def setup(self) -> None:
        """Create the table if absent. Idempotent."""

    @abstractmethod
    async def get(
        self, criterion_value: str, patient_term: str, model_id: str
    ) -> TermRecord | None:
        """One cached mapping by (criterion, term, model)."""

    @abstractmethod
    async def get_many(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        """Cached verdicts for candidate (criterion_value, patient_term) pairs."""

    @abstractmethod
    async def set_many(self, records: Sequence[TermRecord]) -> None:
        """Persist term mapping records into durable storage."""

    @abstractmethod
    async def purge(self, *, model_id: str | None = None) -> int:
        """Purge cached term mappings. Returns how many rows were removed."""

    @abstractmethod
    async def count(self, *, model_id: str | None = None) -> int:
        """Total cached term mappings stored."""

    def get_cached(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        """Synchronous read for sync node callers (default empty)."""
        return {}

    def set_cached(self, records: Sequence[TermRecord]) -> None:
        """Synchronous write for sync node callers (default no-op)."""
        return None


_CREATE_TERM_MAPPINGS_TABLE = """
CREATE TABLE IF NOT EXISTS term_mappings (
    criterion_value TEXT NOT NULL,
    patient_term    TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (criterion_value, patient_term, model_id)
)
"""


class InMemoryTermStore(TermStore):
    """Dict-backed term mapping cache for tests — no durability, no I/O."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], TermRecord] = {}

    async def setup(self) -> None:
        return None

    def get_cached(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        """Synchronous read for in-memory / testing callers."""
        results = {}
        for cval, term in pairs:
            record = self._rows.get((cval, term, model_id))
            if record is not None:
                results[(cval, term)] = record.verdict
        return results

    def set_cached(self, records: Sequence[TermRecord]) -> None:
        """Synchronous write for in-memory / testing callers."""
        for r in records:
            self._rows[(r.criterion_value, r.patient_term, r.model_id)] = r

    async def get(
        self, criterion_value: str, patient_term: str, model_id: str
    ) -> TermRecord | None:
        return self._rows.get((criterion_value, patient_term, model_id))

    async def get_many(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        return self.get_cached(pairs, model_id)

    async def set_many(self, records: Sequence[TermRecord]) -> None:
        self.set_cached(records)

    async def purge(self, *, model_id: str | None = None) -> int:
        if model_id is None:
            total = len(self._rows)
            self._rows.clear()
            return total
        to_delete = [k for k in self._rows if k[2] == model_id]
        for k in to_delete:
            del self._rows[k]
        return len(to_delete)

    async def count(self, *, model_id: str | None = None) -> int:
        if model_id is None:
            return len(self._rows)
        return sum(1 for k in self._rows if k[2] == model_id)


class SqliteTermStore(TermStore):
    """aiosqlite-backed term mapping cache, sharing the store's autocommit connection."""

    def __init__(self, conn: aiosqlite.Connection, db_path: str = "") -> None:
        self._conn = conn
        self._db_path = db_path

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TERM_MAPPINGS_TABLE)
        await self._conn.commit()

    def get_cached(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        """Synchronous read for sync node callers."""
        if not pairs or not self._db_path:
            return {}
        results: dict[tuple[str, str], str] = {}
        pair_list = list(pairs)
        with sqlite3.connect(self._db_path, timeout=30.0) as sync_conn:
            for i in range(0, len(pair_list), 100):
                chunk = pair_list[i : i + 100]
                placeholders = " OR ".join(
                    ["(criterion_value = ? AND patient_term = ?)"] * len(chunk)
                )
                params: list[str] = []
                for cval, term in chunk:
                    params.extend([cval, term])
                params.append(model_id)
                query = (
                    f"SELECT criterion_value, patient_term, verdict FROM term_mappings "
                    f"WHERE ({placeholders}) AND model_id = ?"
                )
                cur = sync_conn.execute(query, tuple(params))
                for row in cur.fetchall():
                    results[(row[0], row[1])] = row[2]
        return results

    def set_cached(self, records: Sequence[TermRecord]) -> None:
        """Synchronous write for sync node callers."""
        if not records or not self._db_path:
            return
        with sqlite3.connect(self._db_path, timeout=30.0) as sync_conn:
            sync_conn.executemany(
                "INSERT INTO term_mappings "
                "(criterion_value, patient_term, model_id, verdict, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(criterion_value, patient_term, model_id) DO UPDATE SET "
                "verdict = excluded.verdict, created_at = excluded.created_at",
                [
                    (r.criterion_value, r.patient_term, r.model_id, r.verdict, r.created_at)
                    for r in records
                ],
            )
            sync_conn.commit()

    async def get(
        self, criterion_value: str, patient_term: str, model_id: str
    ) -> TermRecord | None:
        async with self._conn.execute(
            "SELECT criterion_value, patient_term, model_id, verdict, created_at "
            "FROM term_mappings WHERE criterion_value = ? AND patient_term = ? AND model_id = ?",
            (criterion_value, patient_term, model_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TermRecord(row[0], row[1], row[2], row[3], row[4])

    async def get_many(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        if not pairs:
            return {}
        results: dict[tuple[str, str], str] = {}
        pair_list = list(pairs)
        for i in range(0, len(pair_list), 100):
            chunk = pair_list[i : i + 100]
            placeholders = " OR ".join(["(criterion_value = ? AND patient_term = ?)"] * len(chunk))
            params: list[str] = []
            for cval, term in chunk:
                params.extend([cval, term])
            params.append(model_id)
            query = (
                f"SELECT criterion_value, patient_term, verdict FROM term_mappings "
                f"WHERE ({placeholders}) AND model_id = ?"
            )
            async with self._conn.execute(query, tuple(params)) as cur:
                rows = await cur.fetchall()
                for row in rows:
                    results[(row[0], row[1])] = row[2]
        return results

    async def set_many(self, records: Sequence[TermRecord]) -> None:
        if not records:
            return
        await self._conn.executemany(
            "INSERT INTO term_mappings "
            "(criterion_value, patient_term, model_id, verdict, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(criterion_value, patient_term, model_id) DO UPDATE SET "
            "verdict = excluded.verdict, created_at = excluded.created_at",
            [
                (r.criterion_value, r.patient_term, r.model_id, r.verdict, r.created_at)
                for r in records
            ],
        )
        await self._conn.commit()

    async def purge(self, *, model_id: str | None = None) -> int:
        if model_id is not None:
            async with self._conn.execute(
                "DELETE FROM term_mappings WHERE model_id = ?", (model_id,)
            ) as cur:
                deleted = cur.rowcount
        else:
            async with self._conn.execute("DELETE FROM term_mappings") as cur:
                deleted = cur.rowcount
        await self._conn.commit()
        return deleted

    async def count(self, *, model_id: str | None = None) -> int:
        if model_id is not None:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM term_mappings WHERE model_id = ?", (model_id,)
            ) as cur:
                row = await cur.fetchone()
        else:
            async with self._conn.execute("SELECT COUNT(*) FROM term_mappings") as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


class PostgresTermStore(TermStore):
    """psycopg-backed term mapping cache for production. Same schema, %s placeholders."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TERM_MAPPINGS_TABLE)
        await self._conn.commit()

    async def get(
        self, criterion_value: str, patient_term: str, model_id: str
    ) -> TermRecord | None:
        cur = await self._conn.execute(
            "SELECT criterion_value, patient_term, model_id, verdict, created_at "
            "FROM term_mappings WHERE criterion_value = %s AND patient_term = %s AND model_id = %s",
            (criterion_value, patient_term, model_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return TermRecord(row[0], row[1], row[2], row[3], row[4])

    async def get_many(
        self, pairs: Sequence[tuple[str, str]], model_id: str
    ) -> dict[tuple[str, str], str]:
        if not pairs:
            return {}
        results: dict[tuple[str, str], str] = {}
        pair_list = list(pairs)
        for i in range(0, len(pair_list), 100):
            chunk = pair_list[i : i + 100]
            placeholders = " OR ".join(
                ["(criterion_value = %s AND patient_term = %s)"] * len(chunk)
            )
            params: list[str] = []
            for cval, term in chunk:
                params.extend([cval, term])
            params.append(model_id)
            query = (
                f"SELECT criterion_value, patient_term, verdict FROM term_mappings "
                f"WHERE ({placeholders}) AND model_id = %s"
            )
            cur = await self._conn.execute(query, tuple(params))
            rows = await cur.fetchall()
            for row in rows:
                results[(row[0], row[1])] = row[2]
        return results

    async def set_many(self, records: Sequence[TermRecord]) -> None:
        if not records:
            return
        await self._conn.executemany(
            "INSERT INTO term_mappings "
            "(criterion_value, patient_term, model_id, verdict, created_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (criterion_value, patient_term, model_id) DO UPDATE SET "
            "verdict = EXCLUDED.verdict, created_at = EXCLUDED.created_at",
            [
                (r.criterion_value, r.patient_term, r.model_id, r.verdict, r.created_at)
                for r in records
            ],
        )
        await self._conn.commit()

    async def purge(self, *, model_id: str | None = None) -> int:
        if model_id is not None:
            cur = await self._conn.execute(
                "DELETE FROM term_mappings WHERE model_id = %s", (model_id,)
            )
            deleted = cur.rowcount
        else:
            cur = await self._conn.execute("DELETE FROM term_mappings")
            deleted = cur.rowcount
        await self._conn.commit()
        return deleted
        return int(deleted) if deleted is not None else 0

    async def count(self, *, model_id: str | None = None) -> int:
        if model_id is not None:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM term_mappings WHERE model_id = %s", (model_id,)
            )
            row = await cur.fetchone()
        else:
            cur = await self._conn.execute("SELECT COUNT(*) FROM term_mappings")
            row = await cur.fetchone()
        return row[0] if row else 0
        return int(row[0]) if row else 0


# --- Lifecycle --------------------------------------------------------------


@dataclass
class Persistence:
    """Bundles the checkpointer and every store with their lifecycle.

    Built once per process in the app lifespan; ``aclose`` releases every
    connection on shutdown.
    """

    backend: str
    checkpointer: BaseCheckpointSaver
    store: ScreeningStore
    audit: AuditStore
    rules: RuleStore
    terms: TermStore
    _closers: list[Callable[[], Awaitable[None]]]

    async def aclose(self) -> None:
        for close in self._closers:
            await close()


async def open_persistence(settings: Settings) -> Persistence:
    """Open connections, create tables, and wire up checkpointer + stores."""
    backend = settings.checkpoint_backend
    log.info("persistence.opening", backend=backend)

    if backend == "memory":
        checkpointer: BaseCheckpointSaver = MemorySaver()
        store: ScreeningStore = InMemoryScreeningStore()
        audit: AuditStore = InMemoryAuditStore()
        rules: RuleStore = InMemoryRuleStore()
        terms: TermStore = InMemoryTermStore()
        await store.setup()
        await audit.setup()
        await rules.setup()
        await terms.setup()
        return Persistence(backend, checkpointer, store, audit, rules, terms, [])

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
    rules = SqliteRuleStore(store_conn)
    await rules.setup()
    terms = SqliteTermStore(store_conn, db_path=path)
    await terms.setup()

    return Persistence(
        "sqlite", checkpointer, store, audit, rules, terms, [saver_conn.close, store_conn.close]
    )


async def _open_postgres(settings: Settings) -> Persistence:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection

    dsn = settings.postgres_dsn
    assert dsn is not None  # guaranteed by Settings validation

    # Both autocommit: the saver manages its own transactions, and the store's
    # statements (including SELECTs) must not linger as idle-open transactions.
    saver_conn = await AsyncConnection.connect(dsn, autocommit=True)
    store_conn = await AsyncConnection.connect(dsn, autocommit=True)

    # The ignore is narrow and load-bearing to explain: `AsyncPostgresSaver` is
    # annotated for an `AsyncConnection[dict[str, Any]]` — a connection opened
    # with `row_factory=dict_row` — and this one yields tuples. The saver sets the
    # row factory it needs on its own cursors, so the annotation is stricter than
    # the runtime contract; a full screening was driven through the graph against
    # postgres:16 on exactly this connection and the checkpoint round-tripped.
    #
    # `unused-ignore` rides along because the `postgres` extra is optional: with
    # psycopg installed (CI, #97) the arg-type ignore is required, and without it
    # mypy infers `Any` here and would flag the ignore itself as unused. Naming
    # both codes is what keeps one annotation correct in both installations.
    checkpointer = AsyncPostgresSaver(saver_conn)  # type: ignore[arg-type, unused-ignore]
    await checkpointer.setup()
    store = PostgresScreeningStore(store_conn)
    await store.setup()
    audit = PostgresAuditStore(store_conn)
    await audit.setup()
    rules = PostgresRuleStore(store_conn)
    await rules.setup()
    terms = PostgresTermStore(store_conn)
    await terms.setup()

    return Persistence(
        "postgres", checkpointer, store, audit, rules, terms, [saver_conn.close, store_conn.close]
    )
