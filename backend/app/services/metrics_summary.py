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
    critic_rejections_total,
    parse_attempts,
    screenings_total,
)

# Funnel order: the outcomes a run can end on, worst-last, so the page reads
# "most runs finished, some escalated, a few failed". Any terminal outcome added
# to `TERMINAL_OUTCOMES` but not named here is still shown — appended rather than
# dropped, because a silently missing bar would make the funnel's total look wrong.
_OUTCOME_ORDER = ("done", "escalated", "failed")

# What each outcome is called on screen. `done` is deliberately not "Done": this
# is a funnel of finished work, and "Completed" is what distinguishes it from the
# two outcomes that also finished.
_OUTCOME_LABELS = {
    "done": "Completed",
    "escalated": "Escalated",
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


def summarize_metrics() -> dict[str, Any]:
    """The in-app metrics summary: funnel, Critic rejections, loop depth (#58).

    Cheap enough to serve per request — three `collect()` calls over a handful of
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
    }
