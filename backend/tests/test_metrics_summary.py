"""The in-app metrics summary (#58) — the reduction and the route behind it.

Two halves, and the second is the acceptance criterion. `app/services/
metrics_summary.py` reduces the custom Prometheus metrics into a funnel, a
rejection breakdown and a loop-depth distribution; `GET /api/metrics/summary`
serves them.

The arithmetic tests run against fresh metrics on a private registry
(`isolated`), because prometheus_client's default registry is process-global and
accumulates across the suite — exact shares are only assertable in isolation. The
reconciliation test then drives a real screening through the real registry and
compares the payload against the `/metrics` exposition sample by sample, which is
the criterion that actually matters: a summary whose numbers differ from the
metrics they summarize would be worse than no summary, because a reviewer would
have no way to tell which one lied.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.parser import text_string_to_metric_families

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from app.graph.nodes.critic import SEMANTIC_RULE_ID
from app.services import metrics as metrics_mod
from app.services import metrics_summary
from app.services.metrics import TERMINAL_OUTCOMES
from app.services.metrics_summary import summarize_metrics
from tests.auth_helpers import sign_in
from tests.fakes import PROTOCOL_TEXT, FakeChatModel, bad_criteria

# The buckets `metrics.parse_attempts` is defined with. Restated here so the
# distribution tests can assert exact captions; `test_the_real_metric_uses_the_
# buckets_this_suite_assumes` fails if the definition is ever re-tuned, which is
# what keeps this copy from quietly testing a histogram we no longer ship.
ATTEMPT_BUCKETS = (1, 2, 3, 4, 5, 10)


@pytest.fixture
def isolated(monkeypatch):
    """Fresh copies of the three metrics the summary reads, on a private registry.

    The summary reads its metrics off module globals, so swapping them is enough
    to get a run of the real reduction over numbers this test owns outright — no
    deltas, and shares that mean something.
    """
    registry = CollectorRegistry()
    fakes = SimpleNamespace(
        screenings=Counter("screenings_total", "test", ["outcome"], registry=registry),
        rejections=Counter("critic_rejections_total", "test", ["rule_id"], registry=registry),
        attempts=Histogram("parse_attempts", "test", buckets=ATTEMPT_BUCKETS, registry=registry),
    )
    monkeypatch.setattr(metrics_summary, "screenings_total", fakes.screenings)
    monkeypatch.setattr(metrics_summary, "critic_rejections_total", fakes.rejections)
    monkeypatch.setattr(metrics_summary, "parse_attempts", fakes.attempts)
    return fakes


def _outcomes(payload: dict) -> dict[str, dict]:
    return {row["outcome"]: row for row in payload["funnel"]["outcomes"]}


def _rules(payload: dict) -> dict[str, dict]:
    return {row["rule_id"]: row for row in payload["rejections"]["rules"]}


# --- the funnel --------------------------------------------------------------


def test_the_funnel_counts_terminal_outcomes_and_states_their_shares(isolated):
    for _ in range(3):
        isolated.screenings.labels(outcome="done").inc()
    isolated.screenings.labels(outcome="escalated").inc()
    isolated.screenings.labels(outcome="failed").inc()

    funnel = summarize_metrics()["funnel"]
    assert funnel["total"] == 5
    rows = {row["outcome"]: row for row in funnel["outcomes"]}
    assert rows["done"]["count"] == 3
    assert rows["done"]["share"] == 60.0
    assert rows["escalated"]["share"] == 20.0
    assert rows["failed"]["share"] == 20.0


def test_the_funnel_reads_worst_last(isolated):
    """A funnel that led with failures would misrepresent a healthy instance."""
    assert [row["outcome"] for row in summarize_metrics()["funnel"]["outcomes"]] == [
        "done",
        "escalated",
        "failed",
    ]


def test_every_outcome_the_recorder_counts_has_a_row(isolated):
    """The anti-drift test: a terminal outcome added to `record_node_metrics`'
    vocabulary must appear in the funnel, not vanish out of a total it still
    contributes to."""
    listed = {row["outcome"] for row in summarize_metrics()["funnel"]["outcomes"]}
    assert listed == set(TERMINAL_OUTCOMES)


def test_a_cold_instance_shows_zeroes_rather_than_a_full_bar(isolated):
    """The common state on a fresh process — the counters reset with it. A share
    of no runs is 0%, never 100%."""
    payload = summarize_metrics()
    assert payload["funnel"]["total"] == 0
    assert all(row["count"] == 0 and row["share"] == 0.0 for row in payload["funnel"]["outcomes"])
    assert payload["rejections"] == {"total": 0, "rules": [], "per_run": 0.0}


# --- Critic rejections -------------------------------------------------------


def test_rejections_rank_the_rules_and_state_their_shares(isolated):
    for _ in range(3):
        isolated.rejections.labels(rule_id="RENAL-001").inc()
    isolated.rejections.labels(rule_id="BP-001").inc()

    rejections = summarize_metrics()["rejections"]
    assert [row["rule_id"] for row in rejections["rules"]] == ["RENAL-001", "BP-001"]
    assert rejections["total"] == 4
    assert rejections["rules"][0]["share"] == 75.0
    assert rejections["rules"][1]["share"] == 25.0


def test_tied_rules_are_ordered_by_id_so_the_page_does_not_reshuffle(isolated):
    """The registry hands back its children in first-use order, which would make
    a tie's order depend on which protocol happened to arrive first."""
    isolated.rejections.labels(rule_id="RENAL-001").inc()
    isolated.rejections.labels(rule_id="AGE-001").inc()
    assert [row["rule_id"] for row in summarize_metrics()["rejections"]["rules"]] == [
        "AGE-001",
        "RENAL-001",
    ]


def test_findings_per_run_is_measured_against_the_funnel_total(isolated):
    """The denominator is every run that finished, not just the rejected ones —
    the figure a reviewer reads as "how often does the Critic push back"."""
    for _ in range(4):
        isolated.screenings.labels(outcome="done").inc()
    for _ in range(3):
        isolated.rejections.labels(rule_id="RENAL-001").inc()
    assert summarize_metrics()["rejections"]["per_run"] == 0.75


def test_the_semantic_layer_is_flagged_as_a_model_review(isolated):
    """`LLM-SEM` is not a row of the rules file (services/rules.py lists it
    synthetically), and a breakdown that presented it as a threshold would tell a
    reviewer a model wrote a rule."""
    isolated.rejections.labels(rule_id=SEMANTIC_RULE_ID).inc()
    isolated.rejections.labels(rule_id="BP-001").inc()
    rules = _rules(summarize_metrics())
    assert rules[SEMANTIC_RULE_ID]["layer"] == "semantic"
    assert rules["BP-001"]["layer"] == "deterministic"


# --- parse-attempt depth -----------------------------------------------------


def test_the_distribution_de_cumulates_prometheus_buckets(isolated):
    """Buckets are cumulative in the exposition; the page needs the shape."""
    for value in (1, 1, 1, 3):
        isolated.attempts.observe(value)

    attempts = summarize_metrics()["attempts"]
    assert attempts["observations"] == 4
    assert [(row["label"], row["count"]) for row in attempts["buckets"]] == [
        ("1", 3),
        ("2", 0),
        ("3", 1),
    ]
    assert attempts["mean"] == 1.5
    assert attempts["first_pass_share"] == 75.0


def test_an_empty_bucket_between_two_populated_ones_is_kept(isolated):
    """The gap is a fact about the loop — three attempts but never two says the
    Critic's second pass rarely resolves anything."""
    isolated.attempts.observe(1)
    isolated.attempts.observe(3)
    labels = [row["label"] for row in summarize_metrics()["attempts"]["buckets"]]
    assert labels == ["1", "2", "3"]


def test_buckets_are_captioned_by_attempt_count_not_by_prometheus_bound(isolated):
    """A reviewer is reading how many tries a protocol took, so a bucket is named
    by the attempts that land in it — including the ranges the small-integer
    buckets leave open at the top."""
    for value in (1, 2, 3, 4, 5, 7, 40):
        isolated.attempts.observe(value)
    assert [row["label"] for row in summarize_metrics()["attempts"]["buckets"]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6–10",
        "more than 10",
    ]


def test_trailing_empty_buckets_are_dropped(isolated):
    """MAX_PARSE_ATTEMPTS caps at 10 while the histogram runs to +Inf, so a
    healthy instance would otherwise trail five permanent zeroes."""
    isolated.attempts.observe(1)
    assert [row["label"] for row in summarize_metrics()["attempts"]["buckets"]] == ["1"]


def test_no_resolved_runs_leaves_the_distribution_empty_rather_than_zeroed(isolated):
    """`first_pass_share` is None, not 0: the page omits the claim instead of
    publishing "0% first-pass" about an instance that has screened nothing."""
    attempts = summarize_metrics()["attempts"]
    assert attempts == {
        "observations": 0,
        "mean": 0.0,
        "first_pass_share": None,
        "buckets": [],
    }


def test_the_real_metric_uses_the_buckets_this_suite_assumes():
    """Guards the fixture's copy of the bucket bounds. If `parse_attempts` is
    re-tuned, this fails here rather than letting the caption tests silently
    assert against a histogram we no longer ship."""
    bounds = [
        float(sample.labels["le"])
        for family in metrics_mod.parse_attempts.collect()
        for sample in family.samples
        if sample.name == "parse_attempts_bucket"
    ]
    assert bounds == [*(float(b) for b in ATTEMPT_BUCKETS), float("inf")]


# --- provenance of the numbers -----------------------------------------------


def test_the_window_the_counters_cover_travels_with_the_payload(isolated):
    """Process-scoped counters reported without their epoch would read as
    all-time, which is the one way this page could mislead."""
    since = summarize_metrics()["since"]
    assert since == metrics_mod.COUNTERS_SINCE.isoformat()
    assert datetime.fromisoformat(since).tzinfo is not None


def test_the_payload_says_whether_the_metrics_are_also_scrapable(isolated, monkeypatch):
    """METRICS_ENABLED gates `/metrics`, not the recording — the summary is
    complete either way, and only what it can point at changes."""
    assert summarize_metrics()["exported"] is True
    monkeypatch.setattr(
        metrics_summary, "get_settings", lambda: SimpleNamespace(metrics_enabled=False)
    )
    assert summarize_metrics()["exported"] is False


# --- the route ---------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def test_the_summary_requires_a_session(client):
    assert client.get("/api/metrics/summary").status_code == 401


def test_a_reviewer_can_read_the_summary(client):
    sign_in(client)
    response = client.get("/api/metrics/summary")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"since", "exported", "funnel", "rejections", "attempts"}


# --- reconciliation with /metrics --------------------------------------------


def _exposition(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """The `/metrics` body as `(sample name, sorted labels) -> value`."""
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


async def test_the_summary_reconciles_with_the_prometheus_exposition(monkeypatch):
    """The acceptance criterion, on a real run through the real registry.

    An extraction the Critic never accepts is what makes this worth asserting: it
    populates all three families at once — per-rule rejections, an `escalated`
    outcome, and a loop that resolved deep — so the funnel, the breakdown and the
    distribution are each checked against the exposition rather than against zero.
    """
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([bad_criteria()] * 6))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = str(upload.json()["thread_id"])
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                async for _line in resp.aiter_lines():
                    pass

            # Scrape first, then summarize: nothing between the two requests
            # touches a domain counter, so the two readings describe one instant.
            scraped = _exposition((await client.get("/metrics")).text)
            payload = (await client.get("/api/metrics/summary")).json()

    # Not a vacuous pass: the run above must have populated all three families.
    assert payload["funnel"]["total"] > 0
    assert payload["rejections"]["rules"]
    assert payload["attempts"]["observations"] > 0

    for row in payload["funnel"]["outcomes"]:
        key = ("screenings_total", (("outcome", row["outcome"]),))
        assert scraped.get(key, 0.0) == row["count"], f"funnel disagrees on {row['outcome']}"

    for row in payload["rejections"]["rules"]:
        key = ("critic_rejections_total", (("rule_id", row["rule_id"]),))
        assert scraped[key] == row["count"], f"rejections disagree on {row['rule_id']}"

    assert payload["attempts"]["observations"] == scraped[("parse_attempts_count", ())]
    # The de-cumulated rows must still add up to what was observed, less whatever
    # the trailing-zero trim removed (which is zero by definition).
    assert (
        sum(row["count"] for row in payload["attempts"]["buckets"])
        == (payload["attempts"]["observations"])
    )


async def test_the_funnel_total_counts_the_runs_the_exposition_counts(monkeypatch):
    """The summary's own total is summed over the samples, so it has to equal the
    exposition's — a label the funnel didn't recognize would otherwise inflate one
    side only."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([bad_criteria()] * 6))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            scraped = _exposition((await client.get("/metrics")).text)
            payload = (await client.get("/api/metrics/summary")).json()

    scraped_total = sum(
        value for (name, _labels), value in scraped.items() if name == "screenings_total"
    )
    assert payload["funnel"]["total"] == scraped_total
