"""Durable persistence for screenings (#2).

Two things must survive a restart, crash, or deploy:

1. **LangGraph execution state** — the checkpointer. A run parked at the
   human-approval gate can wait hours; losing it drops the screening.
2. **Screening metadata** — thread_id, filename, status, created_at, plus the
   uploaded protocol text (the input a run streams from). This is what the
   dashboard lists and what a delayed `/stream` rebuilds its input from.

Both live in the *same* database, selected by ``CHECKPOINT_BACKEND``:

- ``memory``   — process-local, lost on restart (tests only).
- ``sqlite``   — durable single-node default.
- ``postgres`` — multi-replica production target (deps in the ``postgres`` extra).

Route handlers never touch SQL: they call the ``ScreeningStore`` repository.
Nothing here is constructed at import — ``AsyncSqliteSaver`` captures the
running loop in its constructor, so everything is built inside ``open_persistence``
from the app's lifespan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    """Metadata row for the list view — never carries the protocol text."""

    thread_id: str
    source_filename: str
    status: str
    created_at: str


@dataclass(frozen=True)
class ScreeningInput:
    """The input a run streams from, rehydrated from the store at stream time."""

    raw_protocol_text: str
    source_filename: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    async def set_status(self, thread_id: str, status: str) -> None: ...

    @abstractmethod
    async def list(self) -> list[ScreeningRecord]:
        """All screenings, newest first."""


class InMemoryScreeningStore(ScreeningStore):
    """Dict-backed store for tests — no durability, no I/O, no event loop needed."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, str]] = {}

    async def setup(self) -> None:
        return None

    async def create(self, thread_id: str, source_filename: str, raw_protocol_text: str) -> None:
        self._rows[thread_id] = {
            "thread_id": thread_id,
            "source_filename": source_filename,
            "raw_protocol_text": raw_protocol_text,
            "status": "routing",
            "created_at": _now(),
        }

    async def exists(self, thread_id: str) -> bool:
        return thread_id in self._rows

    async def get_input(self, thread_id: str) -> ScreeningInput | None:
        row = self._rows.get(thread_id)
        if row is None:
            return None
        return ScreeningInput(row["raw_protocol_text"], row["source_filename"])

    async def set_status(self, thread_id: str, status: str) -> None:
        if thread_id in self._rows:
            self._rows[thread_id]["status"] = status

    async def list(self) -> list[ScreeningRecord]:
        rows = sorted(self._rows.values(), key=lambda r: r["created_at"], reverse=True)
        return [
            ScreeningRecord(r["thread_id"], r["source_filename"], r["status"], r["created_at"])
            for r in rows
        ]


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS screenings (
    thread_id         TEXT PRIMARY KEY,
    source_filename   TEXT NOT NULL,
    raw_protocol_text TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL
)
"""


class SqliteScreeningStore(ScreeningStore):
    """aiosqlite-backed store. Its own connection (WAL) so it never contends
    with the checkpointer's transactions on the shared file."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TABLE)
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

    async def set_status(self, thread_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE screenings SET status = ? WHERE thread_id = ?", (status, thread_id)
        )
        await self._conn.commit()

    async def list(self) -> list[ScreeningRecord]:
        async with self._conn.execute(
            "SELECT thread_id, source_filename, status, created_at "
            "FROM screenings ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [ScreeningRecord(r[0], r[1], r[2], r[3]) for r in rows]


class PostgresScreeningStore(ScreeningStore):
    """psycopg-backed store for production. Same schema, ``%s`` placeholders."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(_CREATE_TABLE)
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

    async def set_status(self, thread_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE screenings SET status = %s WHERE thread_id = %s", (status, thread_id)
        )
        await self._conn.commit()

    async def list(self) -> list[ScreeningRecord]:
        cur = await self._conn.execute(
            "SELECT thread_id, source_filename, status, created_at "
            "FROM screenings ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [ScreeningRecord(r[0], r[1], r[2], r[3]) for r in rows]


# --- Lifecycle --------------------------------------------------------------


@dataclass
class Persistence:
    """Bundles the checkpointer and the metadata store with their lifecycle.

    Built once per process in the app lifespan; ``aclose`` releases every
    connection on shutdown.
    """

    backend: str
    checkpointer: BaseCheckpointSaver
    store: ScreeningStore
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
        await store.setup()
        return Persistence(backend, checkpointer, store, [])

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
    # busy_timeout makes a writer wait out a brief lock instead of erroring.
    saver_conn = await aiosqlite.connect(path)
    await saver_conn.execute("PRAGMA journal_mode=WAL")
    store_conn = await aiosqlite.connect(path)
    for conn in (saver_conn, store_conn):
        await conn.execute("PRAGMA busy_timeout=5000")

    checkpointer = AsyncSqliteSaver(saver_conn)
    await checkpointer.setup()
    store = SqliteScreeningStore(store_conn)
    await store.setup()

    return Persistence("sqlite", checkpointer, store, [saver_conn.close, store_conn.close])


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

    return Persistence("postgres", checkpointer, store, [saver_conn.close, store_conn.close])
