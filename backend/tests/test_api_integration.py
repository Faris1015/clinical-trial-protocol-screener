"""End-to-end API integration (#9): the real ASGI app driven over HTTP with
`httpx.AsyncClient` + `ASGITransport` — the full upload → stream → interrupt →
approve happy path, with only the LLM faked.

The app's lifespan (which wires the in-memory persistence and compiles the
graph — CHECKPOINT_BACKEND=memory is forced in conftest) is entered manually,
because ASGITransport does not emit lifespan events the way a real server does.
Everything else is real: the routes, the service layer, the SSE framing, and
the deterministic Matcher running against the bundled synthetic EHR.
"""

import json

from httpx import ASGITransport, AsyncClient

import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from tests.fakes import FAKE_PATIENTS, PROTOCOL_TEXT, FakeChatModel, good_criteria


def _sse_frames(lines: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")]


async def test_upload_stream_interrupt_approve_happy_path(monkeypatch):
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    # The real Matcher runs on /approve; feed it an in-test EHR (patients.json is
    # a generated, git-ignored artifact, absent in CI).
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Upload a plain-text protocol.
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            assert upload.status_code == 200
            thread_id = upload.json()["thread_id"]

            # 2. Stream the run — it should pause at the human-in-the-loop gate.
            lines: list[str] = []
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                async for line in resp.aiter_lines():
                    lines.append(line)
            frames = _sse_frames(lines)
            nodes = [f["node"] for f in frames]
            assert nodes[:1] == ["router"]
            assert "parser" in nodes and "critic" in nodes
            assert frames[-1]["node"] == "__interrupt__"

            # 3. The dashboard list reflects the paused status.
            listing = (await client.get("/api/screenings")).json()
            assert listing[0]["thread_id"] == thread_id
            assert listing[0]["status"] == "awaiting_approval"

            # 4. State endpoint reports the pending matcher node.
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == ["matcher"]
            assert state["values"]["compliance_passed"] is True

            # 5. Approve — resumes past the gate and runs the real Matcher.
            approve = await client.post(f"/api/screenings/{thread_id}/approve")
            assert approve.status_code == 200
            body = approve.json()
            assert isinstance(body["matched_patients"], list)
            assert len(body["matched_patients"]) > 0
            assert all("patient_id" in p for p in body["matched_patients"])

            # 6. Final status is terminal.
            listing = (await client.get("/api/screenings")).json()
            assert listing[0]["status"] == "done"


async def test_approve_before_streaming_is_conflict(monkeypatch):
    """Approving a screening that has not reached the gate is a clean 409, not a
    crash — the run was never streamed, so there is no interrupt to resume."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            resp = await client.post(f"/api/screenings/{thread_id}/approve")
            assert resp.status_code == 409
            assert resp.json()["error"] == "ScreeningNotApprovableError"
