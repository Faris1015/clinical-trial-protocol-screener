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
        records = await p2.store.list()
        assert [r.thread_id for r in records] == ["t1"]
        assert records[0].status == "awaiting_approval"
        # Metadata rows never carry the protocol text.
        assert not hasattr(records[0], "raw_protocol_text")
    finally:
        await p2.aclose()


async def test_sqlite_store_lists_newest_first(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        await p.store.create("older", "a.pdf", "x")
        await p.store.create("newer", "b.pdf", "y")
        records = await p.store.list()
        # created_at is ISO-8601, so lexical DESC is chronological DESC.
        assert [r.thread_id for r in records] == ["newer", "older"]
    finally:
        await p.aclose()


async def test_missing_thread_is_absent(tmp_path):
    p = await open_persistence(_sqlite_settings(tmp_path))
    try:
        assert not await p.store.exists("nope")
        assert await p.store.get_input("nope") is None
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
        thread_id = client.post(
            "/api/screenings", files={"file": ("p.md", PROTOCOL.encode())}
        ).json()["thread_id"]
        events = _stream_events(client, thread_id)
        assert events[-1]["node"] == "__interrupt__"

    # ---- server 2: brand-new process on the same sqlite file ----
    with TestClient(main.app, raise_server_exceptions=False) as client:
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
        listing = client.get("/api/screenings").json()
        assert listing[0]["thread_id"] == thread_id
        assert listing[0]["status"] == "done"


# --- API surface (memory backend) -------------------------------------------


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def test_list_screenings_returns_metadata_without_protocol_text(client):
    secret = "CONFIDENTIAL-PROTOCOL-BODY"
    client.post("/api/screenings", files={"file": ("trial.md", f"Inclusion: {secret}".encode())})

    response = client.get("/api/screenings")
    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    row = body[0]
    assert set(row) == {"thread_id", "source_filename", "status", "created_at"}
    assert row["source_filename"] == "trial.md"
    assert secret not in response.text


def test_no_module_level_thread_dict():
    # Acceptance criterion: no module-level mutable dict holds screening state.
    assert not hasattr(main, "THREADS")
