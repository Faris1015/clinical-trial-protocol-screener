"""The domain metrics as a summary a reviewer can read (#58).

`app/services/metrics.py` owns the custom Prometheus metrics and `GET /metrics`
exposes them, which answers the operator's question and nobody else's: reading a
screening funnel out of `screenings_total{outcome="escalated"} 3.0` needs a
Prometheus and a dashboard in front of it. This module turns the three figures
the issue names — the terminal-outcome funnel, which rules the Critic blocks on,
and how deep the parse/critic loop runs — into one payload the app can render.
It complements Grafana rather than replacing it: no time series, no percentiles,
no alerting. Those are what a real dashboard is for.

Three decisions worth knowing before editing:

**Read off the live collectors, never recounted.** Every number here comes from
`metric.collect()` on the very objects `/metrics` serializes, so the page and the
scrape cannot disagree — they are one source read twice. The alternative was
counting terminal outcomes out of the screening store, which would have been a
second implementation of "what counts as done" and would have drifted from
`record_node_metrics` the first time either side changed. A metrics summary whose
numbers differ from the metrics is worse than no summary, because a reviewer
would have no way to tell which one lied.

**Process-scoped, and it says so.** These counters live in this process's memory
and reset when it restarts (`COUNTERS_SINCE` is their epoch, and it travels in
the payload). That is a real limitation of reading the registry rather than the
store, and the honest fix is to state the window instead of implying the numbers
are all-time. In the deployed topology that costs nothing: one uvicorn process
serves the app and the API (deploy/demo/Dockerfile), so this registry is the
whole instance.

**Rendered here, not in the browser.** Outcome labels, histogram bucket
captions and every percentage arrive resolved — the same convention
`services/timeline.py` and `services/rules.py` follow. De-cumulating a Prometheus
histogram in particular is knowledge about the exposition format, and it belongs
beside the metric definitions rather than in a component.
"""

from __future__ import annotations

import math
from typing import Any

from prometheus_client.metrics import MetricWrapperBase

from app.config import get_settings
from app.graph.nodes.critic import SEMANTIC_RULE_ID
from app.services.metrics import (
    COUNTERS_SINCE,
    TERMINAL_OUTCOMES,
    agent_node_duration_seconds,
    critic_rejections_total,
    llm_cost_usd_total,
    llm_tokens_total,
    parse_attempts,
    screening_cost_usd,
    screening_node_cost_usd,
    screenings_total,
    term_mapping_llm_pairs_total,
    term_mapping_resolutions_total,
)
from app.services.usage import LLM_NODES

# Funnel order: the outcomes a run can end on, worst-last, so the page reads
# "most runs finished, some escalated, a few were rejected, a few failed". Any
# terminal outcome added to `TERMINAL_OUTCOMES` but not named here is still shown
# — appended rather than dropped, because a silently missing bar would make the
# funnel's total look wrong.
#
# `rejected` (#91) sits before `failed` and not among it on purpose: a reviewer
# deciding a protocol is not screenable is the gate working, and folding that into
# the failure bar would read as an instance breaking once per refused protocol.
_OUTCOME_ORDER = ("done", "escalated", "rejected", "failed")

# What each outcome is called on screen. `done` is deliberately not "Done": this
# is a funnel of finished work, and "Completed" is what distinguishes it from the
# three outcomes that also finished.
_OUTCOME_LABELS = {
    "done": "Completed",
    "escalated": "Escalated",
    "rejected": "Rejected by reviewer",
    "failed": "Failed",
}


def _funnel_outcomes() -> list[str]:
    """Every terminal outcome, the named ones in funnel order first."""
    named = [outcome for outcome in _OUTCOME_ORDER if outcome in TERMINAL_OUTCOMES]
    return named + sorted(TERMINAL_OUTCOMES - set(_OUTCOME_ORDER))


def _outcome_label(outcome: str) -> str:
    return _OUTCOME_LABELS.get(outcome, outcome.replace("_", " ").capitalize())


def _samples(metric: MetricWrapperBase, name: str) -> list[tuple[dict[str, str], float]]:
    """Current `(labels, value)` pairs for one sample name of one metric.

    `collect()` is what the exposition endpoint calls, so this reads exactly the
    numbers a scrape would see at the same instant. Filtered by sample name
    because one metric emits several: a Counter adds `_created`, a Histogram fans
    out into `_bucket`/`_count`/`_sum`.
    """
    return [
        (dict(sample.labels), sample.value)
        for family in metric.collect()
        for sample in family.samples
        if sample.name == name
    ]


def _scalar(metric: MetricWrapperBase, name: str) -> float:
    """The single value of an unlabelled sample, or 0.0 before it exists."""
    samples = _samples(metric, name)
    return samples[0][1] if samples else 0.0


def _count(value: float) -> int:
    """A counter value as the whole number it is.

    Prometheus carries every value as a float; these are only ever incremented by
    one, so rounding is exact rather than lossy — and an integer is what stops the
    page rendering "9 runs" as "9.0".
    """
    return int(round(value))


def _share(count: int, total: int) -> float:
    """`count` as a percentage of `total`, to one decimal. 0.0 when there is no
    total — a share of nothing is not 100%."""
    return round(100 * count / total, 1) if total else 0.0


def _funnel() -> dict[str, Any]:
    """Terminal outcomes and their shares — the screening funnel.

    The total is summed over the samples rather than over the rows below it, so it
    equals what `/metrics` reports even if a future outcome escapes the label map.
    """
    counts = {
        labels.get("outcome", ""): _count(value)
        for labels, value in _samples(screenings_total, "screenings_total")
    }
    total = sum(counts.values())
    return {
        "total": total,
        "outcomes": [
            {
                "outcome": outcome,
                "label": _outcome_label(outcome),
                "count": counts.get(outcome, 0),
                "share": _share(counts.get(outcome, 0), total),
            }
            for outcome in _funnel_outcomes()
        ],
    }


def _rejections(runs: int) -> dict[str, Any]:
    """Which rules the Critic blocks on, most often first.

    `critic_rejections_total` counts *blocking findings*, incremented once per
    finding per Critic pass — so a run sent back twice on the same rule counts
    twice, and one pass that trips two rules counts once for each. That is what
    makes the breakdown useful (it ranks the rules protocols actually fall foul
    of) and why `per_run` is labelled as findings per run rather than as a
    proportion of runs that were rejected, which this counter cannot answer.

    Sorted by count then by id: ties would otherwise reorder between renders,
    since the registry hands back its children in first-use order.
    """
    counts = {
        labels.get("rule_id", ""): _count(value)
        for labels, value in _samples(critic_rejections_total, "critic_rejections_total")
    }
    total = sum(counts.values())
    return {
        "total": total,
        "rules": [
            {
                "rule_id": rule_id,
                "count": count,
                "share": _share(count, total),
                # The semantic layer has no row in the rules file (services/rules.py
                # lists it synthetically). Flagged so the page can say a model raised
                # this rather than a fixed threshold, the distinction #57 draws.
                "layer": "semantic" if rule_id == SEMANTIC_RULE_ID else "deterministic",
            }
            for rule_id, count in sorted(counts.items(), key=lambda row: (-row[1], row[0]))
        ],
        "per_run": round(total / runs, 2) if runs else 0.0,
    }


def _bucket_label(lower_bound: float | None, upper_bound: float) -> str:
    """The caption for one de-cumulated `parse_attempts` bucket.

    Attempt counts are small positive integers, so a bucket is named by the
    attempts that land in it ("1", "2", "6–10") rather than by its Prometheus
    upper bound ("le=10.0") — a reviewer is reading how many tries a protocol
    took, not a histogram. `lower_bound` is the previous bucket's bound, or None
    for the first, whose floor is one attempt.
    """
    if math.isinf(upper_bound):
        # There is always a finite bucket before +Inf; prometheus_client appends it.
        return f"more than {_count(lower_bound)}" if lower_bound is not None else "any"
    if not float(upper_bound).is_integer():
        # Non-integer bounds would make "1–2" a lie about what the bucket holds.
        # Only reachable if `parse_attempts`' buckets are re-tuned to fractions.
        return f"at most {upper_bound:g}"
    low = 1 if lower_bound is None else _count(lower_bound) + 1
    high = _count(upper_bound)
    if low > high:
        return f"at most {high}"
    return str(high) if low == high else f"{low}–{high}"


def _attempts() -> dict[str, Any]:
    """How deep the parse/critic loop ran, as a distribution.

    Prometheus buckets are cumulative, so each row is its bucket minus the one
    below it — the shape of the histogram, which is the question ("how often does
    the Parser get it right first time?"), rather than the running totals the
    exposition carries.

    Only observed for a run whose loop resolved (`record_node_metrics` excludes
    failures), so `observations` is smaller than the funnel total whenever a run
    failed. `first_pass_share` is None rather than 0 when the buckets cannot
    answer it, so the page omits the claim instead of publishing a zero.
    """
    observations = _count(_scalar(parse_attempts, "parse_attempts_count"))
    buckets = sorted(
        (float(labels["le"]), value)
        for labels, value in _samples(parse_attempts, "parse_attempts_bucket")
        if "le" in labels
    )

    rows: list[dict[str, Any]] = []
    previous_cumulative = 0.0
    previous_bound: float | None = None
    for bound, cumulative in buckets:
        count = _count(cumulative - previous_cumulative)
        rows.append(
            {
                "label": _bucket_label(previous_bound, bound),
                "count": count,
                "share": _share(count, observations),
            }
        )
        previous_cumulative, previous_bound = cumulative, bound

    # Trailing empty buckets are dropped, not hidden: `MAX_PARSE_ATTEMPTS` caps at
    # 10 while the buckets run to +Inf, so a healthy instance would otherwise show
    # five zero rows below its data. An empty bucket *between* two populated ones
    # is kept — that gap is a fact about the loop.
    while rows and rows[-1]["count"] == 0:
        rows.pop()

    first_pass = next((row for row in rows if row["label"] == "1"), None)
    return {
        "observations": observations,
        "mean": round(_scalar(parse_attempts, "parse_attempts_sum") / observations, 2)
        if observations
        else 0.0,
        "first_pass_share": first_pass["share"] if first_pass else None,
        "buckets": rows,
    }


# --- Percentiles and cost (#101) --------------------------------------------
#
# The three panels above are distributions a reviewer reads whole. Cost and
# latency are read at a point instead — "what does a screening usually cost",
# "how slow is the Matcher at the tail" — so they are reported as quantiles,
# estimated from the same histograms `/metrics` exposes.


def _buckets(
    metric: MetricWrapperBase, name: str, labels: dict[str, str]
) -> list[tuple[float, float]]:
    """One histogram's cumulative `(upper_bound, count)` pairs, ascending.

    Filtered to the given label set (empty for an unlabelled histogram) so a
    labelled metric's children are read one at a time rather than summed into a
    distribution no single agent ever had.
    """
    return sorted(
        (float(sample_labels["le"]), value)
        for sample_labels, value in _samples(metric, name)
        if "le" in sample_labels
        and all(sample_labels.get(key) == value for key, value in labels.items())
    )


def _quantile(buckets: list[tuple[float, float]], quantile: float) -> float | None:
    """Estimate a quantile from cumulative histogram buckets.

    The standard Prometheus estimate: find the first bucket whose cumulative count
    reaches the target rank, then interpolate linearly between its lower and upper
    bounds. That is an approximation, and the payload labels it as one — an
    exact p95 would need every observation, which is the thing a histogram exists
    not to keep.

    None when there is nothing to estimate from (no observations), or when the
    rank falls in the open-ended `+Inf` bucket, where there is no upper bound to
    interpolate towards and any number would be invented. Callers render the
    absence rather than a zero.
    """
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    rank = quantile * total
    previous_bound = 0.0
    previous_cumulative = 0.0
    for bound, cumulative in buckets:
        if cumulative >= rank:
            if math.isinf(bound):
                return None
            width = cumulative - previous_cumulative
            if width <= 0:
                return round(bound, 6)
            share = (rank - previous_cumulative) / width
            return round(previous_bound + share * (bound - previous_bound), 6)
        previous_bound, previous_cumulative = bound, cumulative
    return None


def _latency() -> list[dict[str, Any]]:
    """Per-node p50/p95 wall-clock, in the app rather than only in Prometheus (#101).

    `agent_node_duration_seconds` has always carried this; until now reading it
    meant a Grafana. The rows are every agent the registry has timed — including
    `router` and `human_escalation`, which make no LLM call but are still nodes a
    reviewer watches — ordered with the LLM-bound ones first because those are
    where the seconds are.
    """
    agents = sorted(
        {
            labels["agent"]
            for labels, _ in _samples(
                agent_node_duration_seconds, "agent_node_duration_seconds_count"
            )
            if "agent" in labels
        },
        key=_node_order,
    )
    rows = []
    for agent in agents:
        buckets = _buckets(
            agent_node_duration_seconds, "agent_node_duration_seconds_bucket", {"agent": agent}
        )
        observations = _count(buckets[-1][1]) if buckets else 0
        if not observations:
            continue
        rows.append(
            {
                "node": agent,
                "runs": observations,
                "p50_seconds": _quantile(buckets, 0.50),
                "p95_seconds": _quantile(buckets, 0.95),
            }
        )
    return rows


def _node_order(node: str) -> tuple[int, str]:
    """Sort key shared by the cost and latency breakdowns: the LLM-bound nodes in
    pipeline order, everything else after, alphabetically."""
    return (LLM_NODES.index(node), node) if node in LLM_NODES else (len(LLM_NODES), node)


def _cost_nodes() -> list[dict[str, Any]]:
    """Cost and tokens per node: the total spent, and the median a screening spends.

    `total_cost_usd` is the counter — every dollar the instance has spent on that
    node since it started. `median_cost_usd` is the per-screening histogram's p50,
    observed only for runs that actually reached the node, so the Matcher's median
    describes screenings that were approved rather than every run ever started.
    Both are here because they answer different questions: the counter says where
    the money went, the median says what one more screening will cost.
    """
    totals: dict[str, dict[str, float]] = {}
    for labels, value in _samples(llm_cost_usd_total, "llm_cost_usd_total"):
        node = labels.get("node", "")
        totals.setdefault(node, {"cost": 0.0, "prompt": 0.0, "completion": 0.0})["cost"] += value
    for labels, value in _samples(llm_tokens_total, "llm_tokens_total"):
        node = labels.get("node", "")
        kind = labels.get("kind", "")
        if kind in ("prompt", "completion"):
            totals.setdefault(node, {"cost": 0.0, "prompt": 0.0, "completion": 0.0})[kind] += value

    rows = []
    for node, figures in sorted(totals.items(), key=lambda item: _node_order(item[0])):
        prompt = _count(figures["prompt"])
        completion = _count(figures["completion"])
        if not prompt and not completion:
            continue
        buckets = _buckets(
            screening_node_cost_usd, "screening_node_cost_usd_bucket", {"node": node}
        )
        rows.append(
            {
                "node": node,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "tokens": prompt + completion,
                # Rounded to the micro-dollar — the precision the stored figure
                # has, and one the page can print without inventing digits.
                "total_cost_usd": round(figures["cost"], 6),
                "median_cost_usd": _quantile(buckets, 0.50),
                "screenings": _count(buckets[-1][1]) if buckets else 0,
            }
        )
    return rows


def _cost() -> dict[str, Any]:
    """What the models cost this instance, and what one screening costs (#101).

    `median_cost_usd` is the middle of `screening_cost_usd`, estimated from its
    buckets — see `_quantile`, and `estimated` in the payload, which says so. It
    is a median rather than a mean because one pathological protocol that looped
    the Critic to the cap would drag a mean somewhere no real screening sits.

    `priced` is whether any spend has been recorded at all. An instance on Ollama
    or the stub reports real tokens and exactly zero dollars, and a page that
    printed "$0.00 median" without saying why would read as a bug rather than as
    the correct answer for a local model.

    `screenings` counts the runs that actually called a model, which is smaller
    than the funnel total whenever the Router turned an upload away — see
    `metrics.record_screening_usage`, which excludes those rather than letting a
    rejection that cost nothing pull the median of the runs that cost something.
    """
    buckets = _buckets(screening_cost_usd, "screening_cost_usd_bucket", {})
    screenings = _count(buckets[-1][1]) if buckets else 0
    total_cost = sum(value for _, value in _samples(llm_cost_usd_total, "llm_cost_usd_total"))
    tokens = {
        kind: _count(
            sum(
                value
                for labels, value in _samples(llm_tokens_total, "llm_tokens_total")
                if labels.get("kind") == kind
            )
        )
        for kind in ("prompt", "completion")
    }
    return {
        "screenings": screenings,
        "calls_priced": total_cost > 0,
        "prompt_tokens": tokens["prompt"],
        "completion_tokens": tokens["completion"],
        "tokens": tokens["prompt"] + tokens["completion"],
        "total_cost_usd": round(total_cost, 6),
        "median_cost_usd": _quantile(buckets, 0.50),
        "p95_cost_usd": _quantile(buckets, 0.95),
        # The exact companion to the estimated median: a histogram keeps its own
        # sum, so the mean is arithmetic rather than interpolation. Both are here
        # because they fail differently — the median shrugs off the one protocol
        # that looped the Critic to the cap, and the mean is right to the
        # micro-dollar. A reader given only an estimate has no way to check it.
        "mean_cost_usd": round(
            _scalar(screening_cost_usd, "screening_cost_usd_sum") / screenings, 6
        )
        if screenings
        else None,
        "nodes": _cost_nodes(),
    }


def _term_mapping() -> dict[str, Any]:
    """The Matcher's term-mapping cache, as a hit rate (#101).

    `resolutions` is how many `(criterion, term)` questions the cohorts screened
    so far required; `llm_pairs` is how many were actually put to a model. The
    difference is what caching saved, and the ratio is the architectural claim
    this feature exists to make checkable: mappings are resolved once per
    screening, not once per patient, so the rate rises with cohort size rather
    than staying flat.

    `hit_rate` is None before anything has been resolved — a share of nothing is
    not 100%, and the page omits the claim rather than publishing a perfect score
    an empty instance did not earn.
    """
    resolutions = _count(_scalar(term_mapping_resolutions_total, "term_mapping_resolutions_total"))
    llm_pairs = _count(_scalar(term_mapping_llm_pairs_total, "term_mapping_llm_pairs_total"))
    # Clamped at zero: `resolutions` is an upper bound on the lookups actually
    # performed (see matcher.TermMappingCost), so it cannot legitimately fall
    # below `llm_pairs` — but a negative rate from a future change to either
    # definition would be worse than a floor.
    served = max(resolutions - llm_pairs, 0)
    return {
        "resolutions": resolutions,
        "llm_pairs": llm_pairs,
        "served_from_cache": served,
        "hit_rate": _share(served, resolutions) if resolutions else None,
    }


def summarize_metrics() -> dict[str, Any]:
    """The in-app metrics summary: funnel, Critic rejections, loop depth (#58),
    cost, per-node latency and the term-mapping cache (#101).

    Cheap enough to serve per request — a handful of `collect()` calls over
    in-memory children, no store round trip and no scrape.
    """
    funnel = _funnel()
    return {
        # The window these counters describe. The page states it, because
        # "9 completed" means something different on a process up for a week.
        "since": COUNTERS_SINCE.isoformat(),
        # Whether `/metrics` is exposed on this instance (METRICS_ENABLED). The
        # counters are recorded either way — `record_node_metrics` is wired into
        # the graph, not into the endpoint — so this summary is complete even when
        # nothing can scrape it. It changes only what the page can point at.
        "exported": get_settings().metrics_enabled,
        "funnel": funnel,
        "rejections": _rejections(funnel["total"]),
        "attempts": _attempts(),
        # Percentile figures are estimated from histogram buckets rather than
        # from the observations themselves. Stated in the payload so the page can
        # say so too: this complements Grafana, and a p95 presented as exact
        # would be the claim that it does not.
        "estimated_percentiles": True,
        "cost": _cost(),
        "latency": _latency(),
        "term_mapping": _term_mapping(),
    }
