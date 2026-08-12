"""The run-level half of the cost accounting (#101) — AC 1 and AC 3.

Where `test_usage.py` pins the arithmetic and the ledger in isolation, this file
drives a real screening through the real graph and asserts the three things a
reviewer actually depends on: every LLM call is attributed to the node that made
it, the record survives the human gate into a durable checkpoint, and the run
detail payload serves it.

The gate is the reason this needs an end-to-end run rather than a unit test. A
screening's bill spans two HTTP requests — the stream that parks it and the
approval that resumes it into the Matcher — so anything that kept the tally in
process memory would look correct in a unit test and lose the Parser's cost the
moment a reviewer walked away for an hour.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from app.schemas.criteria import CriteriaSchema
from app.schemas.review import TermMapping
from app.services import usage
from tests.auth_helpers import sign_in
from tests.fakes import FAKE_PATIENTS, PROTOCOL_TEXT, FakeChatModel, good_criteria


@pytest.fixture
def offline(monkeypatch):
    """A screening that runs to completion with no network and no model.

    The Parser gets a scripted extraction the deterministic Critic accepts, the
    Critic's semantic layer is stubbed out, and the Matcher scores a three-patient
    cohort. What is *not* stubbed is `invoke_with_retry` — the door the accounting
    hangs off — so the Parser's call is a real trip through the recorder.
    """
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)


async def _screen(client, *, approve: bool = True) -> str:
    """Upload, stream to the gate, and (by default) approve through the Matcher."""
    upload = await client.post(
        "/api/screenings",
        files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
    )
    thread_id = str(upload.json()["thread_id"])
    async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
        async for _line in resp.aiter_lines():
            pass
    if approve:
        async with client.stream("POST", f"/api/screenings/{thread_id}/approve", json={}) as resp:
            async for _line in resp.aiter_lines():
                pass
    return thread_id


async def _drive(*, approve: bool = True) -> tuple[str, dict]:
    """Run one screening and return `(thread_id, GET /state payload)`."""
    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            thread_id = await _screen(client, approve=approve)
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            return thread_id, state


async def test_every_call_is_attributed_to_the_node_that_made_it(offline):
    """AC 1: labelled by node. The label comes from the graph's own node table —
    `_instrument` opens the scope — so it cannot drift from the name the duration
    histogram uses for the same node."""
    _thread_id, state = await _drive()

    calls = state["values"]["llm_usage"]
    assert calls, "the Parser made a real call through invoke_with_retry"
    assert all(call["node"] in usage.LLM_NODES for call in calls)
    assert "parser" in {call["node"] for call in calls}
    # Never the fallback label: a call made inside a node must not be filed as
    # unattributed, which is what a broken scope would look like.
    assert usage.UNATTRIBUTED_NODE not in {call["node"] for call in calls}


async def test_the_bill_survives_the_human_gate(offline):
    """AC 3, and the reason this is a checkpoint field rather than a counter.

    The Parser runs on the stream request; the Matcher runs on the approval, which
    is a separate request that may arrive hours later. Both must appear in one
    run's bill.
    """
    _thread_id, parked = await _drive(approve=False)
    parked_calls = parked["values"]["llm_usage"]
    assert parked["pending"] == ["matcher"]
    assert {call["node"] for call in parked_calls} == {"parser"}

    # And a full run — through the gate — still carries what the Parser spent
    # before the reviewer ever looked at it.
    _thread_id, finished = await _drive()
    assert "parser" in {call["node"] for call in finished["values"]["llm_usage"]}
    assert finished["values"]["current_step"] == "done"


async def test_state_serves_the_bill_beside_the_checkpoint(offline):
    """AC 3: "shown on the run detail view" — derived server-side, on the payload
    that view already fetches, so the panel renders numbers rather than deriving
    them in the browser."""
    _thread_id, state = await _drive()

    bill = state["usage"]
    assert bill["calls"] == len(state["values"]["llm_usage"])
    assert bill["tokens"] > 0
    assert [node["node"] for node in bill["nodes"]] == sorted(
        (node["node"] for node in bill["nodes"]),
        key=lambda node: usage.LLM_NODES.index(node),
    )
    # The rollup and the raw record are one derivation, so their calls agree.
    assert sum(node["calls"] for node in bill["nodes"]) == bill["calls"]


async def test_the_runs_index_reports_the_same_bill_it_stored(offline):
    """AC 3: the denormalized columns. The index must not load a checkpoint per
    row, so the figure is stored — and a stored figure that disagreed with the
    detail view would be worse than no figure at all."""
    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            thread_id = await _screen(client)
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            listing = (await client.get("/api/screenings")).json()

    row = next(item for item in listing["items"] if item["thread_id"] == thread_id)
    assert row["llm_tokens"] == state["usage"]["tokens"]
    assert row["llm_cost_usd"] == state["usage"]["cost_usd"]


async def test_a_run_that_never_streamed_has_an_empty_bill():
    """The shape that tells the panel to render nothing at all, rather than a row
    of zeros that would read as a free screening."""
    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = str(upload.json()["thread_id"])
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()

    assert state["usage"]["calls"] == 0
    assert state["usage"]["nodes"] == []
    assert state["usage"]["priced"] is False


async def test_the_record_carries_no_prompt_or_completion_text(offline):
    """PHI-safe by construction, like the approval trail: token counts and a
    price, never the text that produced them. A protocol is not patient data, but
    a completion from the Matcher is about one — and the checkpoint is the one
    place this record would be durable."""
    _thread_id, state = await _drive()

    for call in state["values"]["llm_usage"]:
        assert set(call) == {
            "node",
            "provider",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "cost_micro_usd",
            "estimated",
        }


async def test_the_split_names_every_node_that_spent(monkeypatch):
    """The per-node breakdown with more than one node in it.

    The default fixture's cohort has no categorical terms, so the Matcher settles
    everything deterministically and never calls a model — correct, and exactly
    the behaviour the architecture claims, but it leaves the split one row long.
    A protocol with an ambiguous categorical criterion is what puts a second node
    in the breakdown, so the ordering and the totals are asserted against a real
    two-node run rather than a degenerate one.
    """
    # Built through the schema rather than by assigning onto a model: the graph
    # reads `model_dump()`, and a raw dict poked into a field would sail past
    # validation here and fail three nodes later as an AttributeError.
    criteria = CriteriaSchema(
        **{
            **good_criteria().model_dump(),
            "inclusion_categorical": [
                {
                    "category": "diagnosis",
                    "value": "prior platinum chemotherapy",
                    "negated": False,
                    "source_text": "Prior platinum chemotherapy.",
                }
            ],
        }
    )
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([criteria]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(
        matcher_mod,
        "load_patients",
        lambda: [{**FAKE_PATIENTS[0], "medications": ["carboplatin, 2023-04"]}],
    )
    monkeypatch.setattr(matcher_mod, "get_llm", lambda: FakeChatModel([TermMapping(results=[])]))

    _thread_id, state = await _drive()

    bill = state["usage"]
    nodes = [node["node"] for node in bill["nodes"]]
    assert nodes == ["parser", "matcher"], "pipeline order, and both nodes spent"
    assert sum(node["calls"] for node in bill["nodes"]) == bill["calls"]
    assert sum(node["tokens"] for node in bill["nodes"]) == bill["tokens"]
    assert sum(node["cost_micro_usd"] for node in bill["nodes"]) == bill["cost_micro_usd"]
