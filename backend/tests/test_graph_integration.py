"""Full-graph integration tests (#9): the real compiled graph, a real in-memory
checkpointer, and a scripted FakeChatModel — no network, no LLM, deterministic.

These exercise the wiring the unit tests can't: the Critic→Parser loop, the
escalation cap, the Router reject edge, and the resume-through-matcher path
past the human-in-the-loop interrupt. The Parser is the only LLM-touching node,
so patching `app.graph.nodes.parser.get_llm` is enough to make the whole run
free and repeatable.
"""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
from app.config import get_settings
from app.graph.builder import build_graph
from app.graph.state import initial_state
from tests.fakes import (
    FAKE_PATIENTS,
    NON_PROTOCOL_TEXT,
    PROTOCOL_TEXT,
    FakeChatModel,
    bad_criteria,
    good_criteria,
)


def _graph_with_llm(monkeypatch, scripted: list) -> tuple[Any, FakeChatModel]:
    fake = FakeChatModel(scripted)
    monkeypatch.setattr(parser_mod, "get_llm", lambda: fake)
    # The Critic's layer-2 LLM pass is exercised in test_critic_semantic; here we
    # stub it so these tests drive the loop off the deterministic layer alone.
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    graph = build_graph(MemorySaver())
    return graph, fake


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def _run_to_pause(graph: Any, thread_id: str, text: str = PROTOCOL_TEXT) -> Any:
    """Stream the graph from a fresh state until it ends or interrupts."""
    config = _config(thread_id)
    async for _chunk in graph.astream(
        initial_state(text, "protocol.md"), config, stream_mode="updates"
    ):
        pass
    return await graph.aget_state(config)


def _agent_statuses(events: list[dict], agent: str) -> list[str]:
    return [e["status"] for e in events if e["agent"] == agent]


# --- Critic → Parser loop convergence --------------------------------------


async def test_loop_converges_after_one_rejection(monkeypatch):
    """Bad-then-good extraction: exactly two parse attempts, a rejection then a
    pass from the Critic in that order, and the graph parked at the HITL gate."""
    graph, fake = _graph_with_llm(monkeypatch, [bad_criteria(), good_criteria()])

    snapshot = await _run_to_pause(graph, "converge")
    values = snapshot.values

    assert fake.calls == 2
    assert values["parse_attempts"] == 2
    # The Critic rejected the first extraction, then accepted the second.
    assert _agent_statuses(values["events"], "critic") == ["rejected", "completed"]
    # Two Parser completions, one per attempt.
    assert _agent_statuses(values["events"], "parser") == ["completed", "completed"]
    # Paused before the matcher for human approval — not finished, not escalated.
    assert snapshot.next == ("matcher",)
    assert values["current_step"] == "awaiting_approval"
    assert values["compliance_passed"] is True


# --- Escalation cap --------------------------------------------------------


async def test_always_bad_extraction_escalates_at_the_cap(monkeypatch):
    """Extraction the Critic always rejects: the loop terminates at
    human_escalation after MAX_PARSE_ATTEMPTS instead of looping forever."""
    graph, fake = _graph_with_llm(monkeypatch, [bad_criteria()])
    max_attempts = get_settings().max_parse_attempts

    snapshot = await _run_to_pause(graph, "escalate")
    values = snapshot.values

    assert fake.calls == max_attempts
    assert values["parse_attempts"] == max_attempts
    assert values["current_step"] == "escalated"
    # The run ended (no pending node) at the escalation terminal.
    assert snapshot.next == ()
    assert _agent_statuses(values["events"], "critic")[-1] == "escalated"
    # The Critic rejected on every attempt before giving up.
    assert _agent_statuses(values["events"], "critic").count("rejected") == max_attempts


# --- Router rejection ------------------------------------------------------


async def test_router_rejects_non_protocol_without_calling_parser(monkeypatch):
    """Non-protocol input fails cleanly at the Router; the Parser never runs and
    no LLM call is made."""
    graph, fake = _graph_with_llm(monkeypatch, [good_criteria()])

    snapshot = await _run_to_pause(graph, "reject", text=NON_PROTOCOL_TEXT)
    values = snapshot.values

    assert fake.calls == 0
    assert values["current_step"] == "failed"
    assert snapshot.next == ()
    assert _agent_statuses(values["events"], "router") == ["rejected"]


# --- Happy path: interrupt then resume through the matcher -----------------


async def test_happy_path_pauses_then_resumes_through_matcher(monkeypatch):
    """A clean extraction pauses at the gate; resuming runs the real Matcher
    against the bundled synthetic EHR and completes."""
    graph, fake = _graph_with_llm(monkeypatch, [good_criteria()])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)
    config = _config("happy")

    snapshot = await _run_to_pause(graph, "happy")
    assert fake.calls == 1
    assert snapshot.next == ("matcher",)
    assert snapshot.values["current_step"] == "awaiting_approval"

    # Resume past interrupt_before=["matcher"]; None means "continue".
    result = await graph.ainvoke(None, config)
    assert result["current_step"] == "done"
    assert isinstance(result["matched_patients"], list)
    assert len(result["matched_patients"]) > 0
    assert _agent_statuses(result["events"], "matcher") == ["completed"]
