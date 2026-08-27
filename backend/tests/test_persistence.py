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
from app.persistence import (
    AuditDecision,
    InMemoryTermStore,
    RuleRecord,
    SqliteScreeningStore,
    TermRecord,
    open_persistence,
)
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
        await p.store.set_status(
            "t1",
            "awaiting_approval",
            criteria_count=5,
            match_count=0,
            coverage_checkable=4,
            coverage_criteria=6,
        )
        await p.store.set_status("t1", "failed")
        row = (await p.store.list(limit=10, offset=0)).items[0]
        assert (row.status, row.criteria_count) == ("failed", 5)
        # Coverage (#93) is preserved by the same COALESCE: a run that failed after
        # the gate still has the screenability its extraction earned.
        assert (row.coverage_checkable, row.coverage_criteria) == (4, 6)
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
        # Backfilled to the column default rather than exploding — including the
        # coverage pair added later (#93), which reads as "never scored".
        assert (row.criteria_count, row.match_count) == (0, 0)
        assert (row.coverage_checkable, row.coverage_criteria) == (0, 0)
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

    def invoke(self, _messages: object, *_args: object, **_kwargs: object) -> dict:
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
        # The screenability score (#93), rebuilt from the row's own columns.
        "coverage",
        # The run's LLM bill (#101), rebuilt the same way.
        "llm_tokens",
        "llm_cost_usd",
        # Gate and reminder timestamps (#103).
        "gate_entered_at",
        "last_reminder_at",
    }
    assert set(row["coverage"]) == {"checkable", "criteria", "score"}
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


# --- the rules table, on sqlite (#97) ----------------------------------------


async def test_sqlite_rules_survive_reopen(tmp_path):
    """The point of moving the rules into the database at all: an admin's rule has
    to still be there after a restart, and it has to come back with its bounds and
    keywords intact rather than as the JSON text they are stored as."""
    settings = _sqlite_settings(tmp_path)
    p = await open_persistence(settings)
    try:
        await p.rules.add(
            RuleRecord(
                id="EGFR-001",
                check="range",
                description="eGFR outside plausible range",
                attribute="egfr",
                keywords=["renal", "kidney"],
                params={"min_plausible": 5, "max_plausible": 150},
                created_by="admin@test.local",
            )
        )
    finally:
        await p.aclose()

    p = await open_persistence(settings)
    try:
        (rule,) = await p.rules.list()
        assert rule.id == "EGFR-001"
        assert rule.keywords == ["renal", "kidney"]
        assert rule.params == {"min_plausible": 5, "max_plausible": 150}
        assert rule.enabled is True
        assert rule.created_by == "admin@test.local"
    finally:
        await p.aclose()


async def test_sqlite_seed_only_fills_an_empty_table(tmp_path):
    """A redeploy re-runs seeding, and must not revert an admin's work."""
    settings = _sqlite_settings(tmp_path)
    p = await open_persistence(settings)
    try:
        first = RuleRecord(id="A-001", check="required_attribute", description="a", attribute="age")
        assert await p.rules.seed([first]) == 1
        second = RuleRecord(
            id="B-001", check="required_attribute", description="b", attribute="age"
        )
        assert await p.rules.seed([second]) == 0
        assert [rule.id for rule in await p.rules.list()] == ["A-001"]
    finally:
        await p.aclose()


async def test_sqlite_seeding_a_duplicate_id_is_ignored_rather_than_raising(tmp_path):
    """`INSERT OR IGNORE`, sqlite's spelling of the conflict-ignoring insert the
    postgres store gets from `ON CONFLICT DO NOTHING`. Same guarantee on both
    engines: a repeated id in a hand-edited `RULES_PATH` file must not take down
    startup, and the first one wins."""
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        written = await p.rules.seed(
            [
                RuleRecord(
                    id="A-001", check="required_attribute", description="winner", attribute="age"
                ),
                RuleRecord(
                    id="A-001", check="required_attribute", description="loser", attribute="age"
                ),
            ]
        )
        assert written == 2  # attempted, not landed — see `RuleStore.seed`
        (rule,) = await p.rules.list()
        assert rule.description == "winner"
    finally:
        await p.aclose()


async def test_sqlite_retirement_is_soft_and_the_row_remains(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.rules.add(
            RuleRecord(id="R-001", check="required_attribute", description="r", attribute="age")
        )
        await p.rules.set_enabled("R-001", False, "admin@test.local", "2026-08-11T00:00:00+00:00")

        # Gone from the engine's view...
        assert await p.rules.list(include_disabled=False) == []
        # ...but still resolvable, which is what keeps a past finding's link alive.
        retired = await p.rules.get("R-001")
        assert retired is not None
        assert retired.enabled is False
        assert retired.updated_by == "admin@test.local"
    finally:
        await p.aclose()


async def test_sqlite_replace_keeps_the_original_authorship(tmp_path):
    """An edit revises a rule; it does not re-create one. Rewriting who first
    authored it would be a lie the audit log could not catch."""
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.rules.add(
            RuleRecord(
                id="R-001",
                check="required_attribute",
                description="before",
                attribute="age",
                position=3,
                created_by="first@test.local",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        await p.rules.replace(
            RuleRecord(
                id="R-001",
                check="required_attribute",
                description="after",
                attribute="age",
                # All four of these are ignored by the UPDATE, deliberately.
                position=99,
                created_by="impostor@test.local",
                created_at="2026-12-31T00:00:00+00:00",
                updated_by="second@test.local",
            )
        )
        rule = await p.rules.get("R-001")
        assert rule is not None
        assert rule.description == "after"
        assert rule.updated_by == "second@test.local"
        assert (rule.created_by, rule.created_at) == (
            "first@test.local",
            "2026-01-01T00:00:00+00:00",
        )
        assert rule.position == 3
    finally:
        await p.aclose()


async def test_sqlite_next_position_appends_after_everything(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        assert await p.rules.next_position() == 0
        await p.rules.add(
            RuleRecord(
                id="R-001", check="required_attribute", description="r", attribute="age", position=7
            )
        )
        assert await p.rules.next_position() == 8
    finally:
        await p.aclose()


async def test_sqlite_audit_setup_migrates_and_backfills_a_pre_existing_index(tmp_path):
    """Upgrading a deployment whose `audit_events` predates the subject columns
    (#98 → #97). The backfill is the part that matters: every row already there is
    a decision about a run, and leaving `subject_id` empty would break the deep
    link on every historical entry."""
    import aiosqlite

    path = tmp_path / "screenings.sqlite"
    legacy = await aiosqlite.connect(path)
    await legacy.execute(
        "CREATE TABLE audit_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, action TEXT NOT NULL, "
        "actor TEXT NOT NULL, actor_role TEXT NOT NULL, occurred_at TEXT NOT NULL, "
        "detail TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, "
        "source_filename TEXT NOT NULL DEFAULT '')"
    )
    await legacy.execute(
        "INSERT INTO audit_events "
        "(thread_id, action, actor, actor_role, occurred_at, detail) "
        "VALUES ('run-old', 'approved', 'a@test.local', 'reviewer', "
        "'2026-01-01T00:00:00+00:00', 'Cleared the gate')"
    )
    await legacy.commit()
    await legacy.close()

    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        (row,) = (await p.audit.list(limit=10, offset=0)).items
        assert row.thread_id == "run-old"
        assert row.subject_kind == "screening"
        # Backfilled from the run it names, not left empty.
        assert row.subject_id == "run-old"
        # And setup() is still idempotent on the now-migrated file — it runs on
        # every boot, and a backfill that re-fired would have to be harmless.
        await p.audit.setup()
        assert (await p.audit.list(limit=10, offset=0)).items[0].subject_id == "run-old"
    finally:
        await p.aclose()


async def test_sqlite_backfill_never_touches_a_rule_entry(tmp_path):
    """A rule mutation carries an empty `thread_id` on purpose. The backfill is
    scoped to screening rows so it can never overwrite the rule id with it."""
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.audit.record(
            AuditDecision(
                thread_id="",
                action="rule_created",
                actor="admin@test.local",
                actor_role="admin",
                occurred_at="2026-08-11T00:00:00+00:00",
                detail="range — 1 ≤ age ≤ 2",
                subject_kind="rule",
                subject_id="AGE-002",
            )
        )
        await p.audit.setup()  # re-runs the backfill
        (row,) = (await p.audit.list(limit=10, offset=0)).items
        assert (row.subject_kind, row.subject_id) == ("rule", "AGE-002")
        assert row.thread_id == ""
    finally:
        await p.aclose()


# --- Stale Reminders and Meta Table Persistence (#103) -----------------------


async def test_sqlite_screening_store_parked_runs_and_reminders(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        # Create runs with various statuses
        await p.store.create("t1", "p1.pdf", "text1")
        await p.store.set_status("t1", "routing")

        await p.store.create("t2", "p2.pdf", "text2")
        await p.store.set_status("t2", "awaiting_approval")
        await p.store.mark_gate_entered("t2", "2026-01-01T10:00:00+00:00")

        await p.store.create("t3", "p3.pdf", "text3")
        await p.store.set_status("t3", "done")

        await p.store.create("t4", "p4.pdf", "text4")
        await p.store.set_status("t4", "escalated")
        await p.store.mark_gate_entered("t4", "2026-01-01T11:00:00+00:00")

        parked = await p.store.list_parked()
        assert len(parked) == 2
        thread_ids = [r.thread_id for r in parked]
        assert "t2" in thread_ids
        assert "t4" in thread_ids
        assert "t1" not in thread_ids
        assert "t3" not in thread_ids

        # Test mark_reminder_sent
        await p.store.mark_reminder_sent("t2", "2026-01-01T12:00:00+00:00")
        r2 = await p.store.get_record("t2")
        assert r2 is not None
        assert r2.gate_entered_at == "2026-01-01T10:00:00+00:00"
        assert r2.last_reminder_at == "2026-01-01T12:00:00+00:00"

        # Test re-entering gate clears last_reminder_at
        await p.store.mark_gate_entered("t2", "2026-01-01T13:00:00+00:00")
        r2_updated = await p.store.get_record("t2")
        assert r2_updated is not None
        assert r2_updated.gate_entered_at == "2026-01-01T13:00:00+00:00"
        assert r2_updated.last_reminder_at is None

        # Test meta store
        assert (await p.store.get_meta("last_digest_at")) is None
        await p.store.set_meta("last_digest_at", "2026-01-01T08:00:00+00:00")
        assert (await p.store.get_meta("last_digest_at")) == "2026-01-01T08:00:00+00:00"
        # Test update (upsert)
        await p.store.set_meta("last_digest_at", "2026-01-02T08:00:00+00:00")
        assert (await p.store.get_meta("last_digest_at")) == "2026-01-02T08:00:00+00:00"
    finally:
        await p.aclose()


async def test_sqlite_migration_adds_gate_columns_and_meta_table(tmp_path):
    """An existing database missing gate_entered_at, last_reminder_at, or app_meta
    must migrate smoothly without data loss during setup()."""
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    # Create legacy table schema without gate columns
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE screenings (
                thread_id         TEXT PRIMARY KEY,
                source_filename   TEXT NOT NULL,
                raw_protocol_text TEXT NOT NULL,
                status            TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                criteria_count    INTEGER NOT NULL DEFAULT 0,
                match_count       INTEGER NOT NULL DEFAULT 0,
                coverage_checkable INTEGER NOT NULL DEFAULT 0,
                coverage_criteria INTEGER NOT NULL DEFAULT 0,
                llm_tokens        INTEGER NOT NULL DEFAULT 0,
                llm_cost_micro_usd INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await conn.execute(
            "INSERT INTO screenings "
            "(thread_id, source_filename, raw_protocol_text, status, created_at) "
            "VALUES ('legacy-run', 'legacy.pdf', 'text', 'awaiting_approval', "
            "'2026-01-01T00:00:00+00:00')"
        )
        await conn.commit()

    settings = Settings(
        _env_file=None,
        checkpoint_backend="sqlite",
        sqlite_path=db_path,
    )
    p = await open_persistence(settings)
    try:
        rec = await p.store.get_record("legacy-run")
        assert rec is not None
        assert rec.thread_id == "legacy-run"
        assert rec.gate_entered_at is None
        assert rec.last_reminder_at is None

        # Can write and read meta
        await p.store.set_meta("test_key", "test_val")
        assert (await p.store.get_meta("test_key")) == "test_val"

        # Can update gate timestamps
        await p.store.mark_gate_entered("legacy-run", "2026-01-01T02:00:00+00:00")
        rec2 = await p.store.get_record("legacy-run")
        assert rec2 is not None
        assert rec2.gate_entered_at == "2026-01-01T02:00:00+00:00"
    finally:
        await p.aclose()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
async def test_term_store_lifecycle(tmp_path, store_kind):
    """Test TermStore get_many, set_many, purge, and count lifecycle."""
    if store_kind == "memory":
        store = InMemoryTermStore()
        await store.setup()
        sync_store = store
    else:
        db_path = str(tmp_path / "terms.db")
        settings = Settings(_env_file=None, checkpoint_backend="sqlite", sqlite_path=db_path)
        p = await open_persistence(settings)
        store = p.terms
        sync_store = store

    # Initially empty
    assert await store.count() == 0
    assert await store.get_many([("nsclc", "lung cancer")], "model-1") == {}
    assert sync_store.get_cached([("nsclc", "lung cancer")], "model-1") == {}

    # Insert records
    records = [
        TermRecord("nsclc", "lung cancer", "model-1", "match", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "adenocarcinoma", "model-1", "match", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "small cell", "model-1", "no_match", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "uncertain term", "model-1", "uncertain", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "lung cancer", "model-2", "match", "2026-01-01T00:00:00+00:00"),
    ]
    if store_kind == "memory":
        await store.set_many(records)
    else:
        sync_store.set_cached(records)

    assert await store.count() == 5
    assert await store.count(model_id="model-1") == 4
    assert await store.count(model_id="model-2") == 1

    # Get single and batch
    res_m1 = await store.get_many(
        [("nsclc", "lung cancer"), ("nsclc", "small cell"), ("nsclc", "absent")], "model-1"
    )
    assert res_m1 == {("nsclc", "lung cancer"): "match", ("nsclc", "small cell"): "no_match"}

    res_sync = sync_store.get_cached(
        [("nsclc", "lung cancer"), ("nsclc", "small cell"), ("nsclc", "absent")], "model-1"
    )
    assert res_sync == {("nsclc", "lung cancer"): "match", ("nsclc", "small cell"): "no_match"}

    # Model isolation
    res_m2 = await store.get_many([("nsclc", "small cell")], "model-2")
    assert res_m2 == {}

    # Purge specific model
    purged_m1 = await store.purge(model_id="model-1")
    assert purged_m1 == 4
    assert await store.count() == 1
    assert await store.count(model_id="model-2") == 1

    # Purge all
    purged_all = await store.purge()
    assert purged_all == 1
    assert await store.count() == 0

    if store_kind == "sqlite":
        await p.aclose()
