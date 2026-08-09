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

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from app.schemas.criteria import CriteriaSchema
from tests.auth_helpers import REVIEWER, sign_in
from tests.fakes import (
    FAKE_PATIENTS,
    PROTOCOL_TEXT,
    FakeChatModel,
    bad_criteria,
    good_criteria,
)


def _sse_frames(lines: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")]


async def test_upload_stream_interrupt_approve_happy_path(monkeypatch):
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    # The real Matcher runs on /approve; feed it an in-test EHR (patients.json is
    # a generated, git-ignored artifact, absent in CI).
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
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

            # 3. The runs index reflects the paused status, and the criteria the
            #    parser found are already denormalized onto the row (#51) — the
            #    gate is a terminal frame, so the summary is written there too.
            listing = (await client.get("/api/screenings")).json()
            assert listing["total"] == 1
            assert listing["items"][0]["thread_id"] == thread_id
            assert listing["items"][0]["status"] == "awaiting_approval"
            assert listing["items"][0]["criteria_count"] > 0
            assert listing["items"][0]["match_count"] == 0

            # 4. State endpoint reports the pending matcher node.
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == ["matcher"]
            assert state["values"]["compliance_passed"] is True

            # 5. Approve — resumes past the gate and STREAMS the real Matcher.
            approve_lines: list[str] = []
            async with client.stream("POST", f"/api/screenings/{thread_id}/approve") as approve:
                assert approve.status_code == 200
                assert approve.headers["content-type"].startswith("text/event-stream")
                async for line in approve.aiter_lines():
                    approve_lines.append(line)
            approve_frames = _sse_frames(approve_lines)
            matcher_frames = [f for f in approve_frames if f["node"] == "matcher"]
            assert matcher_frames, "matcher update should stream"
            matched = matcher_frames[-1]["update"]["matched_patients"]
            assert isinstance(matched, list) and len(matched) > 0
            assert all("patient_id" in p for p in matched)
            assert approve_frames[-1]["node"] == "__end__"

            # 6. Final status is terminal, with the cohort summarized on the row.
            listing = (await client.get("/api/screenings")).json()
            assert listing["items"][0]["status"] == "done"
            eligible = [p for p in matched if p["eligible"] and not p["needs_review"]]
            assert listing["items"][0]["match_count"] == len(eligible)

            # 7. The run's audit trail names who authorized touching patient data
            #    (#50) — read back through the real checkpointer, not the request
            #    that wrote it.
            final = (await client.get(f"/api/screenings/{thread_id}/state")).json()["values"]
            assert final["approved_by"] == REVIEWER.email
            assert final["approved_by_role"] == REVIEWER.role
            assert final["approved_at"]
            approval_events = [
                e for e in final["events"] if e["agent"] == "human" and e["status"] == "approved"
            ]
            assert len(approval_events) == 1
            assert REVIEWER.email in approval_events[0]["detail"]


def _edited(criteria: CriteriaSchema, **changes: object) -> dict:
    """`criteria` as a PATCH body, with the named buckets overridden."""
    body: dict = criteria.model_dump()
    body.update(changes)
    return body


async def test_edit_at_the_gate_reruns_the_critic_and_matches_the_edits(monkeypatch):
    """The whole edit-and-rerun loop (#53), end to end over HTTP.

    Load-bearing assertion: `as_node="parser"` really does rewind the *real*
    compiled graph's cursor from the parked matcher back to the Critic. Everything
    else here — the diff, the revision, the audit entry — is stored state, but that
    rewind is a LangGraph behavior, and if it regressed the run would either resume
    straight into the matcher (skipping compliance re-review) or refuse to move.
    """
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]

            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == ["matcher"]
            assert state["values"]["criteria_revision"] == 0
            # The parser's own age bound, which the reviewer is about to raise.
            parsed = state["values"]["parsed_criteria"]
            assert parsed["inclusion_quantitative"][0]["value"] == 18

            # Raise the age floor to 65 — every fake patient passes at 18, only one
            # does at 65, so the cohort itself proves which criteria the matcher ran
            # against.
            raised = dict(parsed["inclusion_quantitative"][0], value=65)
            edit = {
                "base_revision": 0,
                "criteria": _edited(good_criteria(), inclusion_quantitative=[raised]),
            }
            rerun_lines: list[str] = []
            async with client.stream(
                "PATCH", f"/api/screenings/{thread_id}/criteria", json=edit
            ) as rerun:
                assert rerun.status_code == 200
                assert rerun.headers["content-type"].startswith("text/event-stream")
                async for line in rerun.aiter_lines():
                    rerun_lines.append(line)
            rerun_frames = _sse_frames(rerun_lines)

            # The Critic ran again over the edited criteria, and the run parked at
            # the gate rather than resuming into the matcher unapproved.
            assert "critic" in [f["node"] for f in rerun_frames]
            assert rerun_frames[-1]["node"] == "__interrupt__"

            after = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert after["pending"] == ["matcher"]
            assert after["values"]["criteria_revision"] == 1
            assert after["values"]["parsed_criteria"]["inclusion_quantitative"][0]["value"] == 65

            # The audit trail: one revision, its before/after diff, and who made it.
            (revision,) = after["values"]["criteria_edits"]
            assert revision["revision"] == 1
            assert revision["edited_by"] == REVIEWER.email
            assert revision["edited_by_role"] == REVIEWER.role
            (change,) = revision["changes"]
            assert change["kind"] == "modified"
            assert change["before"] == "age >= 18 years"
            assert change["after"] == "age >= 65 years"
            edit_events = [
                e
                for e in after["values"]["events"]
                if e["agent"] == "human" and e["status"] == "edited"
            ]
            assert len(edit_events) == 1
            assert REVIEWER.email in edit_events[0]["detail"]

            # A second edit against the now-stale revision 0 is refused rather than
            # silently overwriting the first reviewer's correction.
            stale = await client.patch(f"/api/screenings/{thread_id}/criteria", json=edit)
            assert stale.status_code == 409
            assert stale.json()["error"] == "CriteriaRevisionConflictError"

            # Approve, and the matcher scores the cohort against the EDITED floor.
            approve_lines: list[str] = []
            async with client.stream("POST", f"/api/screenings/{thread_id}/approve") as approve:
                async for line in approve.aiter_lines():
                    approve_lines.append(line)
            approve_frames = _sse_frames(approve_lines)
            matched = [f for f in approve_frames if f["node"] == "matcher"][-1]["update"][
                "matched_patients"
            ]
            eligible = {p["patient_id"] for p in matched if p["eligible"]}
            # PT-3 (71) clears age >= 65; PT-1 (30) and PT-2 (52) no longer do.
            assert eligible == {"PT-3"}


async def test_escalated_run_can_be_fixed_by_hand_and_rerun(monkeypatch):
    """The blocked path, which had no exit before (#53): the Critic→Parser loop
    exhausts its attempts and escalates, and the reviewer's edit is what gets the
    run moving again."""
    # Always the same bad extraction, so the graph loops to its escalation cap.
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([bad_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                lines = [line async for line in resp.aiter_lines()]
            assert "human_escalation" in [f["node"] for f in _sse_frames(lines)]

            escalated = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert escalated["pending"] == []  # nothing left to resume into
            assert escalated["values"]["current_step"] == "escalated"

            # The reviewer resolves the blocking finding by hand: the vague
            # organ-function sentence becomes a real numeric threshold, which is
            # exactly what HEPATIC-001 was asking for.
            sentence = escalated["values"]["parsed_criteria"]["unparseable"][0]
            fixed = {
                "base_revision": 0,
                "criteria": _edited(
                    good_criteria(),
                    inclusion_quantitative=[
                        *good_criteria().model_dump()["inclusion_quantitative"],
                        {
                            "attribute": "anc",
                            "operator": ">=",
                            "value": 1.5,
                            "value_high": None,
                            "unit": "10^9/L",
                            "source_text": sentence,
                        },
                    ],
                ),
            }
            async with client.stream(
                "PATCH", f"/api/screenings/{thread_id}/criteria", json=fixed
            ) as rerun:
                assert rerun.status_code == 200
                frames = _sse_frames([line async for line in rerun.aiter_lines()])

            # An escalated run resumes: the Critic passes this time, so it lands at
            # the approval gate instead of escalating again.
            assert frames[-1]["node"] == "__interrupt__"
            after = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert after["pending"] == ["matcher"]
            assert after["values"]["compliance_passed"] is True
            listing = (await client.get("/api/screenings")).json()
            assert listing["items"][0]["status"] == "awaiting_approval"
            # The reclassification is on the record, not just the outcome.
            kinds = {c["kind"] for c in after["values"]["criteria_edits"][0]["changes"]}
            assert "reclassified" in kinds


async def test_edit_that_still_fails_compliance_escalates_and_stays_editable(monkeypatch):
    """A reviewer's edit is not privileged. If the Critic still rejects it, the run
    escalates again — and remains editable, so the next attempt isn't blocked.

    The terminal frame is `__end__`: escalation is the graph doing its job, not a
    failure. The `human_escalation` node frame is what tells the UI the re-run was
    blocked, which is the distinction the editor's outcome banner reads."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([bad_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]

            # An edit that leaves the vague organ-function sentence unparsed, so
            # HEPATIC-001 fires exactly as before.
            unchanged = {"base_revision": 0, "criteria": _edited(bad_criteria())}
            async with client.stream(
                "PATCH", f"/api/screenings/{thread_id}/criteria", json=unchanged
            ) as rerun:
                frames = _sse_frames([line async for line in rerun.aiter_lines()])
            assert "human_escalation" in [f["node"] for f in frames]
            assert frames[-1]["node"] == "__end__"

            after = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert after["values"]["current_step"] == "escalated"
            assert after["values"]["criteria_revision"] == 1
            # Still editable at the next revision — a rejected re-run must not trap
            # the run, or the reviewer's second attempt would be a 409.
            retry = await client.patch(
                f"/api/screenings/{thread_id}/criteria",
                json={"base_revision": 1, "criteria": _edited(bad_criteria())},
            )
            assert retry.status_code == 200


async def test_edit_on_a_finished_run_reopens_it_without_its_cohort(monkeypatch):
    """A finished run is editable since #95 — that is what promoting a what-if is.

    End to end, through the real graph: the edit re-runs the Critic, the run parks
    at the gate for a fresh named approval, and the cohort scored under the old
    criteria is gone rather than left standing beneath the new ones.
    """
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]
            async with client.stream("POST", f"/api/screenings/{thread_id}/approve") as approve:
                assert [line async for line in approve.aiter_lines()]
            scored = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert scored["values"]["matched_patients"]

            async with client.stream(
                "PATCH",
                f"/api/screenings/{thread_id}/criteria",
                json={"base_revision": 0, "criteria": _edited(good_criteria())},
            ) as response:
                assert response.status_code == 200
                assert [line async for line in response.aiter_lines()]

            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == ["matcher"]
            assert state["values"]["criteria_revision"] == 1
            assert state["values"]["matched_patients"] == []
            assert state["screening"]["match_count"] == 0
            # And with no cohort there is no attrition to render either — the panel
            # disappears rather than describing a run that no longer has one.
            assert state["attrition"]["criteria"] == []


async def test_edit_on_a_rejected_run_is_still_conflict(monkeypatch):
    """The terminal state editing must not reopen (#91)."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]
            rejected = await client.post(
                f"/api/screenings/{thread_id}/reject",
                json={"reason": "Wrong document."},
            )
            assert rejected.status_code == 200

            response = await client.patch(
                f"/api/screenings/{thread_id}/criteria",
                json={"base_revision": 0, "criteria": _edited(good_criteria())},
            )
            assert response.status_code == 409
            assert response.json()["error"] == "ScreeningNotEditableError"


async def test_edit_rejects_an_attribute_outside_the_ehr_vocabulary(monkeypatch):
    """Hand-edited criteria are validated as strictly as generated ones — the
    Matcher looks attributes up in the patient record, so an invented one has to be
    a 422 here rather than a silently unmatchable criterion later."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]

            response = await client.patch(
                f"/api/screenings/{thread_id}/criteria",
                json={
                    "base_revision": 0,
                    "criteria": _edited(
                        good_criteria(),
                        inclusion_quantitative=[
                            {
                                "attribute": "favourite_colour",
                                "operator": ">=",
                                "value": 1,
                                "value_high": None,
                                "unit": "n/a",
                                "source_text": "Invented.",
                            }
                        ],
                    ),
                },
            )
            assert response.status_code == 422
            # The run is untouched — still parked at the gate on revision 0.
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == ["matcher"]
            assert state["values"]["criteria_revision"] == 0


async def test_edit_before_streaming_is_conflict(monkeypatch):
    """No checkpoint means no extraction to correct — a clean 409, not a crash."""
    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            response = await client.patch(
                f"/api/screenings/{thread_id}/criteria",
                json={"base_revision": 0, "criteria": _edited(good_criteria())},
            )
            assert response.status_code == 409
            assert response.json()["error"] == "ScreeningNotEditableError"


async def test_approve_before_streaming_is_conflict(monkeypatch):
    """Approving a screening that has not reached the gate is a clean 409, not a
    crash — the run was never streamed, so there is no interrupt to resume."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            resp = await client.post(f"/api/screenings/{thread_id}/approve")
            assert resp.status_code == 409
            assert resp.json()["error"] == "ScreeningNotApprovableError"


async def _park_at_the_gate(client) -> str:
    """Upload a protocol and stream it until it interrupts at the human gate."""
    upload = await client.post(
        "/api/screenings",
        files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
    )
    thread_id = str(upload.json()["thread_id"])
    async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
        lines = [line async for line in resp.aiter_lines()]
    assert _sse_frames(lines)[-1]["node"] == "__interrupt__"
    return thread_id


async def test_reject_at_the_gate_ends_the_run_on_the_record(monkeypatch):
    """The gate's other exit, end to end (#91) — through the real checkpointer, so
    this is what proves the run actually *terminates* rather than staying parked
    with a rejection written next to a pending matcher."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            thread_id = await _park_at_the_gate(client)

            reason = "Phase 0 device trial — this cohort has no data for its criteria."
            response = await client.post(
                f"/api/screenings/{thread_id}/reject", json={"reason": reason}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "rejected"
            assert response.json()["rejected_by"] == REVIEWER.email

            # Nothing is pending any more: this is the fix for a run sitting in
            # `awaiting_approval` forever, and it can only be checked against a
            # real graph.
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == []
            values = state["values"]
            assert values["current_step"] == "rejected"
            assert values["rejected_by"] == REVIEWER.email
            assert values["rejected_by_role"] == REVIEWER.role
            assert values["rejected_at"]
            assert values["rejected_reason"] == reason
            # The matcher never ran, so no patient was ever scored.
            assert not values.get("matched_patients")
            # Nor was it mistaken for an approval.
            assert values["approved_by"] is None

            # The event log carries it, attributed to the reviewer rather than to
            # the Critic's own `rejected` push-backs.
            rejections = [
                e for e in values["events"] if e["agent"] == "human" and e["status"] == "rejected"
            ]
            assert len(rejections) == 1
            assert reason in rejections[0]["detail"]

            # And the derived timeline names the actor from the durable trail.
            entry = [
                e
                for e in state["timeline"]["entries"]
                if e["agent"] == "human" and e["status"] == "rejected"
            ][-1]
            assert entry["actor"] == REVIEWER.email
            assert entry["outcome"] == "Rejected"
            assert state["timeline"]["summary"]["rejected_by"] == REVIEWER.email
            assert state["timeline"]["summary"]["rejected_reason"] == reason


async def test_a_rejected_run_is_listed_and_filterable_as_rejected(monkeypatch):
    """The runs index has to stop counting it as in flight — which means both the
    row and the status filter accept the new terminal value (#91)."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            thread_id = await _park_at_the_gate(client)
            await client.post(
                f"/api/screenings/{thread_id}/reject", json={"reason": "Not screenable."}
            )

            row = (await client.get("/api/screenings")).json()["items"][0]
            assert row["status"] == "rejected"
            # The criteria it did extract stay on the row; the cohort is empty.
            assert row["criteria_count"] > 0
            assert row["match_count"] == 0

            filtered = (await client.get("/api/screenings?status=rejected")).json()
            assert [item["thread_id"] for item in filtered["items"]] == [thread_id]
            assert (await client.get("/api/screenings?status=awaiting_approval")).json()[
                "total"
            ] == 0


async def test_a_rejected_run_accepts_no_further_gate_decisions(monkeypatch):
    """Rejection is terminal: approving, re-rejecting or editing it afterwards
    would each rewrite a decision that has already been recorded."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            thread_id = await _park_at_the_gate(client)
            await client.post(
                f"/api/screenings/{thread_id}/reject", json={"reason": "Wrong document."}
            )

            again = await client.post(
                f"/api/screenings/{thread_id}/reject", json={"reason": "Still wrong."}
            )
            assert again.status_code == 409
            assert again.json()["error"] == "ScreeningNotRejectableError"

            approve = await client.post(f"/api/screenings/{thread_id}/approve")
            assert approve.status_code == 409
            assert approve.json()["error"] == "ScreeningNotApprovableError"

            edit = await client.patch(
                f"/api/screenings/{thread_id}/criteria",
                json={"base_revision": 0, "criteria": _edited(good_criteria())},
            )
            assert edit.status_code == 409
            assert edit.json()["error"] == "ScreeningNotEditableError"

            # And the first decision is still the one on file, unamended.
            values = (await client.get(f"/api/screenings/{thread_id}/state")).json()["values"]
            assert values["rejected_reason"] == "Wrong document."


async def test_reject_without_a_reason_is_422(monkeypatch):
    """The reason is the whole point of recording the decision, so a blank one is
    refused before the run is touched — whitespace included."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            thread_id = await _park_at_the_gate(client)

            for body in ({}, {"reason": ""}, {"reason": "   "}):
                response = await client.post(f"/api/screenings/{thread_id}/reject", json=body)
                assert response.status_code == 422, body

            # Untouched: still parked, still approvable.
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["pending"] == ["matcher"]
            assert state["values"].get("rejected_by") is None


async def test_reject_of_a_run_that_never_streamed_is_conflict(monkeypatch):
    """Nothing has asked a reviewer anything yet — the same shape of 409 approving
    such a run gets."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            response = await client.post(
                f"/api/screenings/{upload.json()['thread_id']}/reject",
                json={"reason": "No."},
            )
            assert response.status_code == 409
            assert response.json()["error"] == "ScreeningNotRejectableError"


async def test_an_escalated_run_can_be_rejected_rather_than_fixed(monkeypatch):
    """The blocked path's other exit (#91). An escalated run has already reached
    END, so this covers the branch that merges the decision without moving the
    cursor — the one case `as_node="matcher"` would be wrong for."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([bad_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                lines = [line async for line in resp.aiter_lines()]
            assert "human_escalation" in [f["node"] for f in _sse_frames(lines)]

            response = await client.post(
                f"/api/screenings/{thread_id}/reject",
                json={"reason": "The protocol's eligibility section is unusable as written."},
            )
            assert response.status_code == 200

            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            # Still terminal, and now terminal for a reason a person owns: the
            # escalation stands in the log, the rejection is what closed it.
            assert state["pending"] == []
            assert state["values"]["current_step"] == "rejected"
            assert state["values"]["rejected_by"] == REVIEWER.email
            assert any(e["status"] == "escalated" for e in state["values"]["events"])
            assert (await client.get("/api/screenings")).json()["items"][0]["status"] == "rejected"
