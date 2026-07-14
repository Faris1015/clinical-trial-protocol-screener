"""API error contract (#4): domain errors map to JSON bodies, SSE terminates with __error__."""

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.exceptions import DataStoreError


@pytest.fixture
def client():
    # `with` runs the lifespan so the persistence store is wired up.
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.iter_lines()
        if line.startswith("data: ")
    ]


# --- unknown thread_id → 404 JSON ------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/screenings/unknown-id/stream"),
        ("GET", "/api/screenings/unknown-id/state"),
        ("POST", "/api/screenings/unknown-id/approve"),
    ],
)
def test_unknown_thread_id_returns_404_json(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "ScreeningNotFoundError"
    assert "unknown-id" in body["detail"]


# --- upload errors ----------------------------------------------------------


def test_corrupt_pdf_upload_returns_422(client):
    response = client.post(
        "/api/screenings", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "ExtractionError"
    assert "traceback" not in response.text.lower()


def test_plain_text_upload_still_works(client):
    response = client.post(
        "/api/screenings", files={"file": ("protocol.md", b"Inclusion criteria: age >= 18")}
    )
    assert response.status_code == 200
    assert "thread_id" in response.json()


# --- SSE stream failure → terminal __error__ event --------------------------


class FakeSnapshot:
    def __init__(self, pending: tuple = ()):
        self.next = pending
        self.values: dict = {}


class FakeGraph:
    """Streams one update, then dies with `exc`; approve path re-raises too."""

    def __init__(self, exc: Exception, pending: tuple = ("matcher",)):
        self.exc = exc
        self.pending = pending

    async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[dict]:
        yield {"router": {"current_step": "parsing"}}
        raise self.exc

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return FakeSnapshot(self.pending)

    async def ainvoke(self, *_args: object) -> dict:
        raise self.exc


@pytest.fixture
def thread_id(client):
    response = client.post(
        "/api/screenings", files={"file": ("p.md", b"Inclusion criteria: age >= 18")}
    )
    return response.json()["thread_id"]


def test_stream_emits_terminal_error_event_on_domain_error(client, thread_id, monkeypatch):
    monkeypatch.setattr(main, "graph", FakeGraph(DataStoreError("rules file is corrupt")))
    with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
        events = _sse_events(response)
    assert events[0]["node"] == "router"
    assert events[-1]["node"] == "__error__"
    assert "rules file is corrupt" in events[-1]["message"]


def test_stream_hides_detail_of_unexpected_errors(client, thread_id, monkeypatch):
    monkeypatch.setattr(main, "graph", FakeGraph(RuntimeError("secret internal detail")))
    with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
        events = _sse_events(response)
    assert events[-1]["node"] == "__error__"
    assert "secret internal detail" not in events[-1]["message"]


class CompletingGraph:
    """Streams updates and finishes normally; final state is `values`."""

    def __init__(self, updates: list[dict], values: dict, pending: tuple = ()):
        self.updates = updates
        self.values = values
        self.pending = pending

    async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[dict]:
        for update in self.updates:
            yield update

    async def aget_state(self, _config: object) -> FakeSnapshot:
        snap = FakeSnapshot(self.pending)
        snap.values = self.values
        return snap


def test_stream_absorbed_failure_becomes_terminal_error(client, thread_id, monkeypatch):
    # Parser catches an LLM outage, writes a `failed` step, and routes to END
    # (no exception). The stream must still surface it as __error__, not __end__.
    graph = CompletingGraph(
        updates=[{"parser": {"current_step": "failed"}}],
        values={
            "current_step": "failed",
            "events": [
                {"agent": "parser", "status": "failed", "detail": "LLM backend unavailable"}
            ],
        },
    )
    monkeypatch.setattr(main, "graph", graph)
    with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
        events = _sse_events(response)
    assert events[-1]["node"] == "__error__"
    assert events[-1]["message"] == "LLM backend unavailable"


def test_stream_success_terminates_with_end(client, thread_id, monkeypatch):
    graph = CompletingGraph(
        updates=[{"matcher": {"current_step": "done"}}],
        values={"current_step": "done", "events": []},
    )
    monkeypatch.setattr(main, "graph", graph)
    with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
        events = _sse_events(response)
    assert events[-1]["node"] == "__end__"


def test_stream_awaiting_approval_terminates_with_interrupt(client, thread_id, monkeypatch):
    graph = CompletingGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        values={"current_step": "awaiting_approval", "events": []},
        pending=("matcher",),
    )
    monkeypatch.setattr(main, "graph", graph)
    with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
        events = _sse_events(response)
    assert events[-1]["node"] == "__interrupt__"


# --- approve: datastore failure → 503, screening stays approvable -----------


def test_approve_with_corrupt_patient_store_returns_503(client, thread_id, monkeypatch):
    monkeypatch.setattr(main, "graph", FakeGraph(DataStoreError("patients.json is corrupt")))
    response = client.post(f"/api/screenings/{thread_id}/approve")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "DataStoreError"
    assert "patients.json" in body["detail"]
    # The server is still alive and the screening is still parked at the gate
    assert client.get(f"/api/screenings/{thread_id}/state").status_code == 200


def test_approve_not_awaiting_returns_409(client, thread_id, monkeypatch):
    monkeypatch.setattr(main, "graph", FakeGraph(DataStoreError("x"), pending=()))
    response = client.post(f"/api/screenings/{thread_id}/approve")
    assert response.status_code == 409
