"""Metrics & telemetry (#7): the /metrics endpoint and the custom domain metrics.

The endpoint test asserts every custom family is exposed with the right type
(the acceptance criterion). The recording tests drive the real graph with a
faked LLM and assert deltas via the default registry — deltas, not absolutes,
because prometheus_client's registry is process-global and accumulates across
the suite.
"""

from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from tenacity import wait_none

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
import app.services.llm as llm_mod
from tests.fakes import (
    FAKE_PATIENTS,
    PROTOCOL_TEXT,
    FakeChatModel,
    bad_criteria,
    good_criteria,
)

# Exposition TYPE lines for each custom family (Counters keep their explicit
# `_total`; Histograms fan out into `_bucket`/`_count`/`_sum` samples).
CUSTOM_TYPE_LINES = [
    "# TYPE screenings_total counter",
    "# TYPE agent_node_duration_seconds histogram",
    "# TYPE critic_rejections_total counter",
    "# TYPE parse_attempts histogram",
    "# TYPE llm_call_duration_seconds histogram",
    "# TYPE llm_call_failures_total counter",
]


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Current registry value for a sample, or 0.0 if it has no children yet."""
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")


async def _drive_to_gate(client: AsyncClient) -> str:
    """Upload + drain the stream until the run settles; return the thread_id."""
    upload = await client.post(
        "/api/screenings",
        files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
    )
    thread_id = str(upload.json()["thread_id"])
    async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
        async for _line in resp.aiter_lines():
            pass
    return thread_id


async def test_metrics_endpoint_exposes_all_custom_families():
    """`GET /metrics` returns Prometheus text with every custom metric's TYPE."""
    async with _client() as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    for type_line in CUSTOM_TYPE_LINES:
        assert type_line in body, f"missing exposition line: {type_line}"


async def test_screening_records_domain_metrics(monkeypatch):
    """A full happy-path run bumps the outcome, node-duration, parse-attempt,
    and LLM-latency metrics."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    done_before = _sample("screenings_total", {"outcome": "done"})
    parser_runs_before = _sample("agent_node_duration_seconds_count", {"agent": "parser"})
    attempts_before = _sample("parse_attempts_count")
    llm_before = _sample("llm_call_duration_seconds_count", {"provider": "ollama"})

    async with main.lifespan(main.app), _client() as client:
        thread_id = await _drive_to_gate(client)
        approve = await client.post(f"/api/screenings/{thread_id}/approve")
        assert approve.status_code == 200

    assert _sample("screenings_total", {"outcome": "done"}) == done_before + 1
    assert (
        _sample("agent_node_duration_seconds_count", {"agent": "parser"}) == parser_runs_before + 1
    )
    # The parse-attempt histogram is observed once per completed screening.
    assert _sample("parse_attempts_count") == attempts_before + 1
    # The Parser's extraction went through invoke_with_retry → one LLM observation.
    assert _sample("llm_call_duration_seconds_count", {"provider": "ollama"}) >= llm_before + 1


async def test_critic_rejection_and_escalation_metrics(monkeypatch):
    """An extraction that never passes the Critic increments the per-rule
    rejection counter and lands as an `escalated` outcome."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([bad_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    rejects_before = _sample("critic_rejections_total", {"rule_id": "HEPATIC-001"})
    escalated_before = _sample("screenings_total", {"outcome": "escalated"})

    async with main.lifespan(main.app), _client() as client:
        thread_id = await _drive_to_gate(client)
        # A never-converging run escalates rather than reaching the gate.
        state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
        assert state["values"]["current_step"] == "escalated"

    # One rejection per Parser attempt in the loop (MAX_PARSE_ATTEMPTS = 3).
    assert _sample("critic_rejections_total", {"rule_id": "HEPATIC-001"}) >= rejects_before + 1
    assert _sample("screenings_total", {"outcome": "escalated"}) == escalated_before + 1


async def test_transient_llm_outage_counts_as_failure(monkeypatch):
    """A backend outage that exhausts retries bumps llm_call_failures_total and
    the run ends as `failed` — but its aborted attempt count is NOT observed
    into parse_attempts (that histogram tracks loop depth, not failures)."""
    monkeypatch.setattr(
        parser_mod,
        "get_llm",
        lambda: FakeChatModel([ConnectionError("ollama down")]),
    )
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    # Retries are the real backoff; skip the sleeps so the test stays fast.
    monkeypatch.setattr(llm_mod, "_RETRY_WAIT", wait_none())

    failures_before = _sample("llm_call_failures_total", {"provider": "ollama"})
    failed_before = _sample("screenings_total", {"outcome": "failed"})
    attempts_before = _sample("parse_attempts_count")

    async with main.lifespan(main.app), _client() as client:
        thread_id = await _drive_to_gate(client)
        state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
        assert state["values"]["current_step"] == "failed"

    assert _sample("llm_call_failures_total", {"provider": "ollama"}) >= failures_before + 1
    assert _sample("screenings_total", {"outcome": "failed"}) == failed_before + 1
    # A failed run never resolved the loop, so parse_attempts must be untouched.
    assert _sample("parse_attempts_count") == attempts_before


async def test_non_transient_llm_error_is_not_a_backend_failure(monkeypatch):
    """A deterministic bad-output error (ValueError, not transient) means the
    backend answered — it must not inflate llm_call_failures_total."""
    monkeypatch.setattr(
        parser_mod,
        "get_llm",
        lambda: FakeChatModel([ValueError("not retryable")]),
    )
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    failures_before = _sample("llm_call_failures_total", {"provider": "ollama"})

    async with main.lifespan(main.app), _client() as client:
        await _drive_to_gate(client)

    assert _sample("llm_call_failures_total", {"provider": "ollama"}) == failures_before
