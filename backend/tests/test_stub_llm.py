"""The stub LLM provider (#10): zero-inference model wired via LLM_PROVIDER=stub.

Covers the three things load testing depends on — the model answers each graph
call site with the right schema, `get_llm()` selects it, `/ready` reports it
healthy without any backend — plus one end-to-end run proving the whole pipeline
flows to the human-in-the-loop gate and produces matches on approval.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

import app.graph.nodes.matcher as matcher_mod
import app.services.llm as llm_mod
from app.config import get_settings
from app.graph.builder import build_graph
from app.graph.state import initial_state
from app.health import _check_llm
from app.schemas.criteria import CriteriaSchema
from app.schemas.review import SemanticReview, TermMapping
from app.services.stub_llm import StubChatModel
from tests.fakes import FAKE_PATIENTS, PROTOCOL_TEXT


@pytest.fixture(autouse=True)
def _reset_settings():
    get_settings.cache_clear()
    llm_mod.get_llm.cache_clear()
    yield
    get_settings.cache_clear()
    llm_mod.get_llm.cache_clear()


def test_returns_the_schema_each_call_site_asks_for():
    stub = StubChatModel()
    criteria = stub.with_structured_output(CriteriaSchema).invoke([])
    review = stub.with_structured_output(SemanticReview).invoke([])
    mapping = stub.with_structured_output(TermMapping).invoke([])

    assert isinstance(criteria, CriteriaSchema)
    assert isinstance(review, SemanticReview)
    assert isinstance(mapping, TermMapping)
    # Clean extraction -> Critic doesn't loop, Matcher's fast path settles everything.
    assert review.findings == []
    assert mapping.results == []


def test_unknown_schema_is_a_loud_error():
    with pytest.raises(ValueError, match="no canned output"):
        StubChatModel().with_structured_output(dict).invoke([])


def test_latency_is_applied_per_call():
    stub = StubChatModel(latency_seconds=0.05)
    started = time.perf_counter()
    stub.with_structured_output(CriteriaSchema).invoke([])
    assert time.perf_counter() - started >= 0.05


def test_get_llm_selects_the_stub(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    get_settings.cache_clear()
    llm_mod.get_llm.cache_clear()
    assert isinstance(llm_mod.get_llm(), StubChatModel)


async def test_readiness_reports_stub_healthy_without_a_backend(monkeypatch):
    """No Ollama/Anthropic to reach — the probe must pass on the stub alone."""
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    get_settings.cache_clear()
    result = await _check_llm()
    assert result["ok"] is True


async def test_end_to_end_run_reaches_gate_and_matches(monkeypatch):
    """The real compiled graph, driven only by the stub, streams to the HITL
    interrupt and produces matches once resumed — the exact journey the load
    test exercises."""
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    get_settings.cache_clear()
    llm_mod.get_llm.cache_clear()
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: list(FAKE_PATIENTS))

    # `Any` sidesteps CompiledStateGraph's strict astream/ainvoke overloads on the
    # bare config dict (same convention as test_graph_integration).
    graph: Any = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "stub-e2e"}}

    async for _chunk in graph.astream(
        initial_state(PROTOCOL_TEXT, "protocol.md"), config, stream_mode="updates"
    ):
        pass

    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("matcher",)  # parked at the human-in-the-loop gate

    result = await graph.ainvoke(None, config)
    assert result["current_step"] == "done"
    assert len(result["matched_patients"]) == len(FAKE_PATIENTS)
