"""Batch upload (#61): several protocols in one submission, each its own thread.

Two things are worth pinning down here. First, **partial success**: a batch whose
third file is a scanned PDF must still screen the other seven, so a per-file
rejection is reported per item and never fails the submission — while a bad file
*set* (empty, over the cap) is refused whole. Second, that a batched run is not a
second class of run: it streams, lists, and rehydrates exactly like an
individually uploaded one, which is what makes it navigable from history.
"""

import json

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from tests.auth_helpers import sign_in
from tests.fakes import PROTOCOL_TEXT, FakeChatModel, good_criteria

PROTOCOL = b"Inclusion criteria: age >= 18"


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c)
        yield c


def _files(*names: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    """A multipart body with one `files` part per name, in the given order."""
    return [("files", (name, PROTOCOL, "text/markdown")) for name in names]


# --- one thread per file ----------------------------------------------------


def test_batch_creates_one_screening_per_file(client):
    resp = client.post("/api/screenings/batch", files=_files("a.md", "b.md", "c.md"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 3
    assert body["rejected"] == 0
    thread_ids = [item["thread_id"] for item in body["items"]]
    # Distinct threads, not one screening with three protocols in it.
    assert len(set(thread_ids)) == 3
    assert all(item["error"] is None for item in body["items"])


def test_batch_items_echo_the_submission_order(client):
    """The client pairs its file picker's rows with these items by index, so the
    order is part of the contract — not an accident of dict iteration."""
    resp = client.post("/api/screenings/batch", files=_files("first.md", "second.md", "third.md"))
    assert [item["filename"] for item in resp.json()["items"]] == [
        "first.md",
        "second.md",
        "third.md",
    ]


def test_batched_runs_land_in_the_runs_index(client):
    """The acceptance criterion for navigating a batch: each run is a row in
    history, by filename, reachable by its own thread_id."""
    created = client.post("/api/screenings/batch", files=_files("one.md", "two.md")).json()
    listing = client.get("/api/screenings").json()
    assert listing["total"] == 2
    assert {row["source_filename"] for row in listing["items"]} == {"one.md", "two.md"}
    for item in created["items"]:
        state = client.get(f"/api/screenings/{item['thread_id']}/state")
        assert state.status_code == 200
        assert state.json()["screening"]["source_filename"] == item["filename"]


def test_batch_filenames_are_sanitized_before_storage(client):
    resp = client.post(
        "/api/screenings/batch",
        files=[("files", ("../../etc/passwd", PROTOCOL, "text/plain"))],
    )
    assert resp.json()["items"][0]["filename"] == "passwd"
    names = {row["source_filename"] for row in client.get("/api/screenings").json()["items"]}
    assert names == {"passwd"}


# --- partial success --------------------------------------------------------


def test_disallowed_file_is_rejected_without_failing_the_batch(client):
    resp = client.post(
        "/api/screenings/batch",
        files=[
            ("files", ("good.md", PROTOCOL, "text/markdown")),
            ("files", ("logo.png", b"\x89PNG\r\n", "image/png")),
            ("files", ("also-good.md", PROTOCOL, "text/markdown")),
        ],
    )
    # A rejected file is an item, not a status code: the other two screened.
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 2
    assert body["rejected"] == 1
    rejected = body["items"][1]
    assert rejected["thread_id"] is None
    assert rejected["error"] == "UnsupportedMediaTypeError"
    assert rejected["detail"]
    assert [item["thread_id"] is not None for item in body["items"]] == [True, False, True]
    # And the rejected one left nothing behind in history.
    assert client.get("/api/screenings").json()["total"] == 2


def test_unreadable_pdf_is_rejected_per_item(client):
    """The 422 case, through the real PDF path: a .pdf that isn't one.

    This is the failure a batch exists to survive — one bad scan in a folder of
    good protocols — and the item carries the same error name the single-upload
    route would have answered with.
    """
    resp = client.post(
        "/api/screenings/batch",
        files=[
            ("files", ("broken.pdf", b"not a pdf at all", "application/pdf")),
            ("files", ("fine.md", PROTOCOL, "text/markdown")),
        ],
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["error"] == "ExtractionError"
    assert items[1]["thread_id"]


def test_oversized_file_is_rejected_per_item(client, monkeypatch):
    monkeypatch.setattr(main.settings, "max_upload_bytes", 1024)
    resp = client.post(
        "/api/screenings/batch",
        files=[
            ("files", ("small.md", PROTOCOL, "text/markdown")),
            ("files", ("big.md", b"x" * 4096, "text/markdown")),
        ],
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["thread_id"]
    assert items[1]["error"] == "PayloadTooLargeError"


# --- a bad file *set* is refused whole --------------------------------------


def test_batch_over_the_file_cap_is_refused(client):
    names = [f"p{n}.md" for n in range(main.MAX_BATCH_FILES + 1)]
    resp = client.post("/api/screenings/batch", files=_files(*names))
    assert resp.status_code == 422
    assert resp.json()["error"] == "InvalidBatchError"
    # Refused before anything was created — not a half-processed submission.
    assert client.get("/api/screenings").json()["total"] == 0


def test_batch_at_the_file_cap_is_accepted(client):
    names = [f"p{n}.md" for n in range(main.MAX_BATCH_FILES)]
    resp = client.post("/api/screenings/batch", files=_files(*names))
    assert resp.status_code == 200
    assert resp.json()["created"] == main.MAX_BATCH_FILES


def test_batch_with_no_files_is_422(client):
    """Not our check — FastAPI's body validation refuses a submission with no
    `files` part, which is why the route only has to state the ceiling."""
    resp = client.post("/api/screenings/batch", data={})
    assert resp.status_code == 422
    assert client.get("/api/screenings").json()["total"] == 0


def test_oversized_batch_body_is_rejected_by_content_length(client, monkeypatch):
    """The aggregate ceiling: per-file caps alone would let MAX_BATCH_FILES × cap
    through, so the body itself is bounded from its declared size."""
    monkeypatch.setattr(main.settings, "max_upload_bytes", 1024)
    over_aggregate = b"x" * (1024 * main.MAX_BATCH_FILES + 32 * 1024)
    resp = client.post(
        "/api/screenings/batch",
        files=[("files", ("big.md", over_aggregate, "text/markdown"))],
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "PayloadTooLargeError"


# --- guards -----------------------------------------------------------------


def test_batch_requires_a_session():
    with TestClient(main.app, raise_server_exceptions=False) as anonymous:
        resp = anonymous.post("/api/screenings/batch", files=_files("a.md"))
    assert resp.status_code == 401


# --- a batched run is an ordinary run ---------------------------------------


async def test_batched_runs_stream_independently(monkeypatch):
    """Each thread runs its own pipeline to its own gate.

    Streamed one after another, as the batch view does with a bounded number of
    connections — the point being that a batched thread needs no special resume
    path: it is the same `GET /stream` a single upload gets.
    """
    monkeypatch.setattr(
        parser_mod, "get_llm", lambda: FakeChatModel([good_criteria("Batched Trial")])
    )
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            batch = await client.post(
                "/api/screenings/batch",
                files=[
                    ("files", ("trial-a.md", PROTOCOL_TEXT.encode(), "text/markdown")),
                    ("files", ("trial-b.md", PROTOCOL_TEXT.encode(), "text/markdown")),
                ],
            )
            assert batch.status_code == 200
            thread_ids = [item["thread_id"] for item in batch.json()["items"]]

            for thread_id in thread_ids:
                frames = []
                async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            frames.append(json.loads(line.removeprefix("data: ")))
                # Parked at the human gate, like any other run — which is what the
                # batch view reports as this row's terminal state.
                assert frames[-1]["node"] == "__interrupt__"

            listing = (await client.get("/api/screenings")).json()
            assert listing["total"] == 2
            assert {row["status"] for row in listing["items"]} == {"awaiting_approval"}
            assert all(row["criteria_count"] > 0 for row in listing["items"])
