"""Durable persistence (#2): the store and checkpointer survive a restart.

Acceptance criteria exercised here:
- Kill and restart the server mid-screening → the thread is still resumable
  from the interrupt (``test_screening_resumes_from_interrupt_after_restart``).
- Screening metadata + input outlive the process (``test_sqlite_store_*``).
- No module-level mutable dict holds screening state in main.py
  (``test_no_module_level_thread_dict``).

Restart is simulated faithfully: a first ``TestClient`` lifespan streams a
screening to the human-approval gate against a temp sqlite file, that app
shuts down (connections closed), then a *second* lifespan opens the same file
and approves — proving the state came from disk, not process memory.
"""

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import Settings
from app.persistence import SqliteScreeningStore, open_persistence
from tests.auth_helpers import sign_in

# A minimal protocol that clears the router's length + eligibility-marker gate.
PROTOCOL = (
    "Clinical Trial Protocol. Inclusion criteria: patients aged 18 years or older "
    "with a confirmed diagnosis are eligible for enrollment. Exclusion criteria: "
    "pregnancy or any condition the investigator deems unsafe. " + "Additional context. " * 5
)

VALID_CRITERIA = {
    "trial_title": "Test Trial",
    "inclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18,
            "value_high": None,
            "unit": "years",
            "source_text": "aged 18 years or older",
        }
    ],
    "inclusion_categorical": [],
    "exclusion_quantitative": [],
    "exclusion_categorical": [],
    "unparseable": [],
}


def _sqlite_settings(tmp_path) -> Settings:
    # Explicit init kwargs beat the CHECKPOINT_BACKEND=memory env from conftest.
    return Settings(
        _env_file=None,
        checkpoint_backend="sqlite",
        sqlite_path=tmp_path / "screenings.sqlite",
    )


# --- Store durability -------------------------------------------------------


async def test_sqlite_store_survives_reopen(tmp_path):
    settings = _sqlite_settings(tmp_path)

    p1 = await open_persistence(settings)
    await p1.store.create("t1", "proto.pdf", "the raw protocol body")
    await p1.store.set_status("t1", "awaiting_approval")
    await p1.aclose()

    # New process: fresh connections to the same file.
    p2 = await open_persistence(settings)
    try:
        assert await p2.store.exists("t1")
        inp = await p2.store.get_input("t1")
        assert inp is not None
        assert inp.raw_protocol_text == "the raw protocol body"
        assert inp.source_filename == "proto.pdf"
        page = await p2.store.list(limit=10, offset=0)
        assert [r.thread_id for r in page.items] == ["t1"]
        assert page.items[0].status == "awaiting_approval"
        # Metadata rows never carry the protocol text.
        assert not hasattr(page.items[0], "raw_protocol_text")
    finally:
        await p2.aclose()


async def test_sqlite_store_lists_newest_first(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.store.create("older", "a.pdf", "x")
        await p.store.create("newer", "b.pdf", "y")
        page = await p.store.list(limit=10, offset=0)
        # created_at is ISO-8601, so lexical DESC is chronological DESC.
        assert [r.thread_id for r in page.items] == ["newer", "older"]
        assert page.total == 2
    finally:
        await p.aclose()


async def test_sqlite_store_pages_filters_and_searches(tmp_path):
    """The runs index's three list controls (#51), against real SQL rather than
    the in-memory double — LIMIT/OFFSET, the status filter and the LIKE search
    are the parts that only exist in the SQL stores."""
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.store.create("t1", "nsclc-protocol.pdf", "x")
        await p.store.create("t2", "ckd-protocol.pdf", "y")
        await p.store.create("t3", "hfref-protocol.pdf", "z")
        await p.store.set_status("t2", "done", criteria_count=7, match_count=3)

        first = await p.store.list(limit=2, offset=0)
        assert [r.thread_id for r in first.items] == ["t3", "t2"]
        assert first.total == 3
        second = await p.store.list(limit=2, offset=2)
        assert [r.thread_id for r in second.items] == ["t1"]
        assert second.total == 3

        done = await p.store.list(limit=10, offset=0, status="done")
        assert [r.thread_id for r in done.items] == ["t2"]
        assert (done.items[0].criteria_count, done.items[0].match_count) == (7, 3)

        # sqlite's LIKE is ASCII-case-insensitive, which is the behaviour the
        # search box relies on.
        assert [
            r.thread_id for r in (await p.store.list(limit=10, offset=0, search="CKD")).items
        ] == ["t2"]
        # thread_id is searchable too.
        assert [
            r.thread_id for r in (await p.store.list(limit=10, offset=0, search="t3")).items
        ] == ["t3"]
    finally:
        await p.aclose()


async def test_sqlite_search_treats_wildcards_as_literals(tmp_path):
    """A user typing `%` means the character, not "match everything" — otherwise
    the search box is a way to bypass its own filter."""
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.store.create("t1", "100%-cohort.pdf", "x")
        await p.store.create("t2", "plain.pdf", "y")
        assert [
            r.thread_id for r in (await p.store.list(limit=10, offset=0, search="%")).items
        ] == ["t1"]
        # `_` is LIKE's single-character wildcard; it must be literal too.
        assert (await p.store.list(limit=10, offset=0, search="plain_pdf")).total == 0
    finally:
        await p.aclose()


async def test_sqlite_set_status_preserves_counts(tmp_path):
    """`set_status` with no counts must not zero the ones already stored."""
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.store.create("t1", "a.pdf", "x")
        await p.store.set_status("t1", "awaiting_approval", criteria_count=5, match_count=0)
        await p.store.set_status("t1", "failed")
        row = (await p.store.list(limit=10, offset=0)).items[0]
        assert (row.status, row.criteria_count) == ("failed", 5)
    finally:
        await p.aclose()


async def test_sqlite_setup_adds_columns_to_a_pre_existing_table(tmp_path):
    """Upgrading a deployment whose sqlite file predates #51: CREATE TABLE IF NOT
    EXISTS is a no-op there, so setup() has to ALTER in the new columns or every
    subsequent query fails on the missing ones."""
    import aiosqlite

    path = tmp_path / "screenings.sqlite"
    legacy = await aiosqlite.connect(path)
    await legacy.execute(
        "CREATE TABLE screenings ("
        "thread_id TEXT PRIMARY KEY, source_filename TEXT NOT NULL, "
        "raw_protocol_text TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await legacy.execute(
        "INSERT INTO screenings VALUES ('old', 'legacy.pdf', 'body', 'done', '2026-01-01T00:00:00')"
    )
    await legacy.commit()
    await legacy.close()

    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        row = (await p.store.list(limit=10, offset=0)).items[0]
        assert row.thread_id == "old"
        # Backfilled to the column default rather than exploding.
        assert (row.criteria_count, row.match_count) == (0, 0)
        # And setup() is still idempotent on the now-migrated file.
        await p.store.setup()
    finally:
        await p.aclose()


async def test_sqlite_get_record_returns_the_summary_row(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.store.create("t1", "a.pdf", "x")
        await p.store.set_status("t1", "done", criteria_count=6, match_count=2)
        row = await p.store.get_record("t1")
        assert row is not None
        assert (row.source_filename, row.status) == ("a.pdf", "done")
        assert (row.criteria_count, row.match_count) == (6, 2)
        # And it never carries the protocol text.
        assert not hasattr(row, "raw_protocol_text")
    finally:
        await p.aclose()


async def test_missing_thread_is_absent(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        assert not await p.store.exists("nope")
        assert await p.store.get_input("nope") is None
        assert await p.store.get_record("nope") is None
        # Updating a nonexistent row is a no-op, not an error.
        await p.store.set_status("nope", "done")
    finally:
        await p.aclose()


async def test_sqlite_store_connection_is_autocommit(tmp_path):
    """Regression (#10): the store connection MUST be in autocommit mode.

    With Python's default implicit transactions, the shared store connection
    fast-failed writes with "database is locked" under concurrent load (~76% of
    creates at 50 users) — a write promoting an already-open implicit transaction
    takes an immediate SQLITE_BUSY that busy_timeout can't absorb. Autocommit
    (isolation_level=None) makes each write acquire the lock on the path where
    busy_timeout IS honored, which dropped the same load to <0.5% errors. If this
    regresses to a non-None isolation level, the load-test failure returns.
    See docs/performance.md.
    """
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        assert isinstance(p.store, SqliteScreeningStore)
        assert p.store._conn.isolation_level is None
    finally:
        await p.aclose()


# --- End-to-end restart: resume from the interrupt --------------------------


class _ScriptedLLM:
    """Stands in for get_llm().with_structured_output(...) — always VALID_CRITERIA."""

    def with_structured_output(self, _schema: object) -> "_ScriptedLLM":
        return self

    def invoke(self, _messages: object) -> dict:
        return VALID_CRITERIA


@pytest.fixture
def durable_app(tmp_path, monkeypatch):
    """Wire the real graph to a temp sqlite file, with the LLM + rules + EHR
    stubbed so the pipeline is deterministic and self-contained (no network,
    no committed data files)."""
    monkeypatch.setattr(main, "settings", _sqlite_settings(tmp_path))
    monkeypatch.setattr("app.graph.nodes.parser.get_llm", lambda: _ScriptedLLM())
    # Critic approves (its deterministic layer is exercised in test_critic_rules,
    # its LLM layer in test_critic_semantic) — stub both so this test stays offline.
    monkeypatch.setattr("app.graph.nodes.critic.run_deterministic_checks", lambda *a, **k: [])
    monkeypatch.setattr("app.graph.nodes.critic.run_llm_semantic_review", lambda _state: [])

    # A one-patient EHR the matcher can read after restart, kept out of the repo.
    patients = tmp_path / "patients.json"
    patients.write_text(
        json.dumps(
            [
                {
                    "id": "p1",
                    "name": "Alice",
                    "labs": {"age": 25},
                    "diagnoses": [],
                    "medications": [],
                    "history": [],
                }
            ]
        )
    )
    matcher_settings = Settings(
        _env_file=None,
        checkpoint_backend="sqlite",
        sqlite_path=tmp_path / "screenings.sqlite",
        patients_path=patients,
    )
    monkeypatch.setattr("app.graph.nodes.matcher.get_settings", lambda: matcher_settings)
    return tmp_path


def _stream_events(client, thread_id):
    with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
        return [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]


def test_screening_resumes_from_interrupt_after_restart(durable_app):
    # ---- server 1: upload and stream to the approval gate ----
    with TestClient(main.app, raise_server_exceptions=False) as client:
        sign_in(client)
        thread_id = client.post(
            "/api/screenings", files={"file": ("p.md", PROTOCOL.encode())}
        ).json()["thread_id"]
        events = _stream_events(client, thread_id)
        assert events[-1]["node"] == "__interrupt__"

    # ---- server 2: brand-new process on the same sqlite file ----
    with TestClient(main.app, raise_server_exceptions=False) as client:
        sign_in(client)
        # State came from disk, not memory: the gate and parsed criteria survive.
        state = client.get(f"/api/screenings/{thread_id}/state").json()
        assert state["pending"] == ["matcher"]
        assert state["values"]["parsed_criteria"]["trial_title"] == "Test Trial"

        # And approval resumes past the interrupt and STREAMS the matcher.
        with client.stream("POST", f"/api/screenings/{thread_id}/approve") as approved:
            assert approved.status_code == 200
            frames = [
                json.loads(line.removeprefix("data: "))
                for line in approved.iter_lines()
                if line.startswith("data: ")
            ]
        matched = next(f for f in frames if f["node"] == "matcher")["update"]["matched_patients"]
        assert len(matched) == 1
        assert matched[0]["patient_id"] == "p1"

        # The list view reflects the terminal status.
        listing = client.get("/api/screenings").json()["items"]
        assert listing[0]["thread_id"] == thread_id
        assert listing[0]["status"] == "done"


# --- API surface (memory backend) -------------------------------------------


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c)
        yield c


def test_list_screenings_returns_metadata_without_protocol_text(client):
    secret = "CONFIDENTIAL-PROTOCOL-BODY"
    client.post("/api/screenings", files={"file": ("trial.md", f"Inclusion: {secret}".encode())})

    response = client.get("/api/screenings")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert len(body["items"]) >= 1
    row = body["items"][0]
    assert set(row) == {
        "thread_id",
        "source_filename",
        "status",
        "created_at",
        "criteria_count",
        "match_count",
    }
    assert row["source_filename"] == "trial.md"
    assert secret not in response.text


def _upload(client, name: str) -> str:
    body = f"Inclusion criteria: age >= 18. {name}".encode()
    return str(client.post("/api/screenings", files={"file": (name, body)}).json()["thread_id"])


def test_list_endpoint_pages_and_reports_the_total(client):
    for name in ("one.md", "two.md", "three.md"):
        _upload(client, name)

    body = client.get("/api/screenings", params={"limit": 2, "offset": 0}).json()
    assert len(body["items"]) == 2
    assert (body["total"], body["limit"], body["offset"]) == (3, 2, 0)

    tail = client.get("/api/screenings", params={"limit": 2, "offset": 2}).json()
    assert len(tail["items"]) == 1
    assert tail["total"] == 3


def test_list_endpoint_filters_by_status_and_search(client):
    thread_id = _upload(client, "nsclc-trial.md")
    _upload(client, "ckd-trial.md")

    hits = client.get("/api/screenings", params={"q": "NSCLC"}).json()
    assert [r["thread_id"] for r in hits["items"]] == [thread_id]

    # Nothing has run, so everything is still at the create-time status.
    routing = client.get("/api/screenings", params={"status": "routing"}).json()
    assert routing["total"] == 2
    assert client.get("/api/screenings", params={"status": "done"}).json()["total"] == 0


def test_list_endpoint_rejects_out_of_range_and_unknown_filters(client):
    # A typo'd status must be a 422, not a silently empty page that reads as
    # "no runs in that state".
    assert client.get("/api/screenings", params={"status": "finished"}).status_code == 422
    assert client.get("/api/screenings", params={"limit": 0}).status_code == 422
    assert client.get("/api/screenings", params={"limit": 100_000}).status_code == 422
    assert client.get("/api/screenings", params={"offset": -1}).status_code == 422


def test_no_module_level_thread_dict():
    # Acceptance criterion: no module-level mutable dict holds screening state.
    assert not hasattr(main, "THREADS")
