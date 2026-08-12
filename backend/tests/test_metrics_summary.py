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
        "rejected",
        "failed",
    ]


def test_a_reviewer_rejection_is_its_own_bar_not_a_failure(isolated):
    """The funnel has to distinguish "we chose not to screen this" from "we could
    not" (#91) — folding a reviewer's decision into `failed` would read as an
    instance breaking once per refused protocol."""
    isolated.screenings.labels(outcome="done").inc()
    isolated.screenings.labels(outcome="rejected").inc()
    isolated.screenings.labels(outcome="rejected").inc()

    rows = _outcomes(summarize_metrics())
    assert rows["rejected"]["count"] == 2
    assert rows["rejected"]["share"] == 66.7
    assert rows["rejected"]["label"] == "Rejected by reviewer"
    assert rows["failed"]["count"] == 0


def test_record_rejection_counts_the_outcome_the_graph_never_emits(isolated, monkeypatch):
    """No node runs when a reviewer stops a screening, so `record_node_metrics`
    can't see it — `record_rejection` is what keeps the run in the funnel at all."""
    monkeypatch.setattr(metrics_mod, "screenings_total", isolated.screenings)

    metrics_mod.record_rejection({})

    assert _outcomes(summarize_metrics())["rejected"]["count"] == 1


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


# --- cost, latency and the term-mapping cache (#101) -------------------------


@pytest.fixture
def isolated_cost(monkeypatch):
    """Fresh copies of the metrics the cost/latency/cache blocks read.

    Same reason as `isolated`: these are read off module globals, and a private
    registry is what makes an exact median assertable rather than a delta over
    whatever the rest of the suite happened to record.
    """
    registry = CollectorRegistry()
    fakes = SimpleNamespace(
        screening_cost=Histogram(
            "screening_cost_usd", "test", buckets=metrics_mod._COST_BUCKETS, registry=registry
        ),
        node_cost=Histogram(
            "screening_node_cost_usd",
            "test",
            ["node"],
            buckets=metrics_mod._COST_BUCKETS,
            registry=registry,
        ),
        tokens=Counter("llm_tokens_total", "test", ["node", "provider", "kind"], registry=registry),
        cost_total=Counter("llm_cost_usd_total", "test", ["node", "provider"], registry=registry),
        duration=Histogram(
            "agent_node_duration_seconds",
            "test",
            ["agent"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=registry,
        ),
        resolutions=Counter("term_mapping_resolutions_total", "test", registry=registry),
        pairs=Counter("term_mapping_llm_pairs_total", "test", registry=registry),
    )
    for name, fake in (
        ("screening_cost_usd", fakes.screening_cost),
        ("screening_node_cost_usd", fakes.node_cost),
        ("llm_tokens_total", fakes.tokens),
        ("llm_cost_usd_total", fakes.cost_total),
        ("agent_node_duration_seconds", fakes.duration),
        ("term_mapping_resolutions_total", fakes.resolutions),
        ("term_mapping_llm_pairs_total", fakes.pairs),
    ):
        monkeypatch.setattr(metrics_summary, name, fake)
    return fakes


def test_the_summary_reports_a_median_cost_per_screening(isolated_cost):
    """AC 4: the headline figure. A median rather than a mean, so one pathological
    protocol that looped the Critic to the cap can't drag it somewhere no real
    screening sits."""
    for cost in (0.004, 0.006, 0.008, 0.4):
        isolated_cost.screening_cost.observe(cost)
    isolated_cost.cost_total.labels(node="parser", provider="anthropic").inc(0.418)

    cost = summarize_metrics()["cost"]
    assert cost["screenings"] == 4
    assert cost["calls_priced"] is True
    # Three of four runs are under a cent; the median must land there and not be
    # dragged out by the single expensive one.
    assert cost["median_cost_usd"] is not None
    assert cost["median_cost_usd"] < 0.01
    assert cost["total_cost_usd"] == 0.418


def test_a_run_that_never_called_a_model_is_not_in_the_median(isolated_cost, monkeypatch):
    """The Router rejects non-protocol input before the Parser runs, and that
    rejection is terminal. Observing its zero would pull the median toward a
    figure no *screening* ever cost — on an instance that turns away three
    uploads for every real protocol, a two-cent screening reads as a third of a
    cent.
    """
    monkeypatch.setattr(metrics_mod, "screening_cost_usd", isolated_cost.screening_cost)
    monkeypatch.setattr(metrics_mod, "screening_node_cost_usd", isolated_cost.node_cost)

    for _ in range(3):
        metrics_mod.record_screening_usage([])  # router-rejected: no call at all
    metrics_mod.record_screening_usage(
        [
            {
                "node": "parser",
                "prompt_tokens": 5000,
                "completion_tokens": 800,
                "cost_micro_usd": 20_000,
                "estimated": False,
            }
        ]
    )

    cost = summarize_metrics()["cost"]
    assert cost["screenings"] == 1, "only the run that actually called a model"
    assert cost["median_cost_usd"] is not None
    assert cost["median_cost_usd"] > 0.01


def test_a_run_whose_calls_were_unpriced_is_still_in_the_median(isolated_cost, monkeypatch):
    """The other side of the guard: a local model's screening really did cost
    nothing, and that zero is the honest median for the instance. Excluding it
    would leave an Ollama deployment with an empty cost panel and no way to tell
    that from one that has never run.

    Note what the estimator can and cannot say here. A run costing exactly $0
    lands in the first bucket, and interpolating within it yields half that
    bucket's width rather than a true zero — a histogram cannot distinguish $0.00
    from $0.0004. That is precisely why `calls_priced` exists and why the page
    gates on it: an unpriced instance is told it has no billed spend rather than
    shown a bucket artefact as though it were a price.
    """
    monkeypatch.setattr(metrics_mod, "screening_cost_usd", isolated_cost.screening_cost)
    monkeypatch.setattr(metrics_mod, "screening_node_cost_usd", isolated_cost.node_cost)

    metrics_mod.record_screening_usage(
        [
            {
                "node": "parser",
                "prompt_tokens": 5000,
                "completion_tokens": 800,
                "cost_micro_usd": 0,
                "estimated": False,
            }
        ]
    )

    cost = summarize_metrics()["cost"]
    assert cost["screenings"] == 1, "the run was counted, not filtered out with the zeros"
    assert cost["median_cost_usd"] is not None
    assert cost["median_cost_usd"] < metrics_mod._COST_BUCKETS[0]
    # And the figure the page actually keys on is unambiguous.
    assert cost["calls_priced"] is False


def test_the_cost_is_split_by_node(isolated_cost):
    """AC 4: "split by node". The total says where the money went; the median says
    what one more screening will cost at that node."""
    for node, per_run in (("parser", 0.006), ("critic", 0.001), ("matcher", 0.02)):
        isolated_cost.node_cost.labels(node=node).observe(per_run)
        isolated_cost.cost_total.labels(node=node, provider="anthropic").inc(per_run)
        isolated_cost.tokens.labels(node=node, provider="anthropic", kind="prompt").inc(1000)
        isolated_cost.tokens.labels(node=node, provider="anthropic", kind="completion").inc(200)

    rows = {row["node"]: row for row in summarize_metrics()["cost"]["nodes"]}
    # Pipeline order, so a reader follows the run rather than an alphabet.
    assert [row["node"] for row in summarize_metrics()["cost"]["nodes"]] == [
        "parser",
        "critic",
        "matcher",
    ]
    assert rows["parser"]["tokens"] == 1200
    assert rows["matcher"]["total_cost_usd"] == 0.02
    assert rows["critic"]["median_cost_usd"] is not None


def test_an_unpriced_instance_reports_tokens_and_no_money(isolated_cost):
    """AC 2, at the summary level: a local deployment shows real tokens and says
    plainly that nothing here is priced, rather than printing "$0.00 median" as
    though the figure were a measurement of spend."""
    isolated_cost.screening_cost.observe(0.0)
    isolated_cost.tokens.labels(node="parser", provider="ollama", kind="prompt").inc(5000)
    isolated_cost.tokens.labels(node="parser", provider="ollama", kind="completion").inc(900)

    cost = summarize_metrics()["cost"]
    assert cost["tokens"] == 5900
    assert cost["calls_priced"] is False
    assert cost["total_cost_usd"] == 0


def test_the_summary_reports_per_node_latency_percentiles(isolated_cost):
    """AC 5: p50/p95 per node in the app, not only in the Prometheus histogram."""
    for _ in range(19):
        isolated_cost.duration.labels(agent="parser").observe(0.2)
    isolated_cost.duration.labels(agent="parser").observe(4.0)  # one slow tail run
    isolated_cost.duration.labels(agent="router").observe(0.01)

    rows = {row["node"]: row for row in summarize_metrics()["latency"]}
    assert rows["parser"]["runs"] == 20
    assert rows["parser"]["p50_seconds"] is not None
    assert rows["parser"]["p95_seconds"] is not None
    # The tail is what the p95 is for: it must sit well above the median.
    assert rows["parser"]["p95_seconds"] > rows["parser"]["p50_seconds"]
    assert rows["router"]["p50_seconds"] is not None


def test_a_node_with_no_timed_runs_gets_no_row(isolated_cost):
    """An empty row would claim a node that has never run is instantaneous."""
    assert summarize_metrics()["latency"] == []


def test_the_summary_reports_the_term_mapping_cache_hit_rate(isolated_cost):
    """AC 4: the caching claim, aggregated. 500 resolutions the cohorts needed
    against 10 pairs the model was actually asked is a 98% hit rate — and it is
    the shape of that ratio, not its exact value, that the architecture claims."""
    isolated_cost.resolutions.inc(500)
    isolated_cost.pairs.inc(10)

    cache = summarize_metrics()["term_mapping"]
    assert cache["resolutions"] == 500
    assert cache["llm_pairs"] == 10
    assert cache["served_from_cache"] == 490
    assert cache["hit_rate"] == 98.0


def test_the_hit_rate_is_omitted_before_anything_has_been_resolved(isolated_cost):
    """A share of nothing is not 100% — the page omits the claim rather than
    publishing a perfect score an empty instance did not earn."""
    assert summarize_metrics()["term_mapping"]["hit_rate"] is None


def test_a_percentile_with_no_observations_is_none_rather_than_zero(isolated_cost):
    """Zero is a cost; None is the API saying it has nothing to estimate from.
    Conflating them would report an instance that has run nothing as free."""
    cost = summarize_metrics()["cost"]
    assert cost["median_cost_usd"] is None
    assert cost["p95_cost_usd"] is None


def test_the_summary_reports_an_exact_mean_beside_the_estimated_median(isolated_cost):
    """A histogram keeps its own sum, so the mean is arithmetic rather than
    interpolation — and it is the figure a reader can check the estimate against.
    Three runs at 2, 4 and 30 cents: the mean is exactly 12 cents, and the median
    sits near the middle run rather than being dragged out by the expensive one."""
    for cost in (0.02, 0.04, 0.30):
        isolated_cost.screening_cost.observe(cost)
    isolated_cost.cost_total.labels(node="parser", provider="anthropic").inc(0.36)

    cost = summarize_metrics()["cost"]
    assert cost["mean_cost_usd"] == 0.12, "exact, to the micro-dollar"
    assert 0.02 < cost["median_cost_usd"] < 0.06, cost["median_cost_usd"]


def test_the_mean_is_omitted_when_nothing_has_been_observed(isolated_cost):
    """None rather than a division by zero — and rather than a 0 that would read
    as a free instance."""
    assert summarize_metrics()["cost"]["mean_cost_usd"] is None


def test_the_cost_buckets_resolve_the_range_screenings_actually_land_in(isolated_cost):
    """Bucket width is the error bar on the headline figure of this whole feature.

    A quantile read off a histogram interpolates inside whichever bucket the rank
    lands in, so a coarse step through the cents range makes a measured screening
    report a cost it never had — a 0.05→0.1 step reported a $0.054 run as $0.075,
    39% high. This pins the resolution rather than the bucket list: any re-tuning
    is free to move the bounds as long as the estimate stays close to the truth.
    """
    for actual in (0.006, 0.018, 0.054, 0.12):
        isolated_cost.screening_cost.observe(actual)
        estimate = _quantile_of(isolated_cost.screening_cost, 0.50)
        assert estimate is not None
        assert abs(estimate - actual) / actual < 0.25, f"{actual} estimated as {estimate}"
        isolated_cost.screening_cost._sum.set(0)
        for bucket in isolated_cost.screening_cost._buckets:
            bucket.set(0)


def _quantile_of(histogram, quantile: float) -> float | None:
    """The summary's own estimator, run against one histogram directly."""
    buckets = sorted(
        (float(sample.labels["le"]), sample.value)
        for family in histogram.collect()
        for sample in family.samples
        if sample.name.endswith("_bucket")
    )
    return metrics_summary._quantile(buckets, quantile)


def test_the_real_cost_histograms_use_the_buckets_this_suite_assumes():
    """The anti-drift twin of the parse_attempts bucket test: re-tuning
    `_COST_BUCKETS` must fail here rather than quietly changing what the medians
    above are asserting."""
    assert metrics_mod._COST_BUCKETS[0] == 0.0005
    assert metrics_mod._COST_BUCKETS[-1] == 5.0
    assert metrics_mod._COST_BUCKETS == tuple(sorted(metrics_mod._COST_BUCKETS))


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
    # `coverage` (#93) is the one block not read off a collector — it is pooled from
    # recent checkpoints, and tests/test_coverage.py covers it.
    assert set(body) == {
        "since",
        "exported",
        "funnel",
        "rejections",
        "attempts",
        "coverage",
        # The cost accounting (#101): what the models spent, how slow each node
        # is at the tail, and what the term-mapping cache saved.
        "estimated_percentiles",
        "cost",
        "latency",
        "term_mapping",
    }


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
