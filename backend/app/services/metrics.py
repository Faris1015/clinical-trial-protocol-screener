"""Prometheus metric definitions — the single home for every custom metric.

Standard HTTP metrics (request count, latency, in-flight) are added by
`prometheus-fastapi-instrumentator` in `app/main.py`; this module owns the
*domain* metrics that answer the questions plain HTTP timings can't: p95
screening duration, how often the Critic rejects and on which rule, how deep
the self-correction loop runs, and LLM call latency/failures per provider.

Everything registers against prometheus_client's default registry, so defining
each metric exactly once at import time is what keeps a re-import from raising a
duplicate-registration error. Nothing else in the codebase constructs a metric
— nodes and services call the objects (or the helpers) declared here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import Counter, Histogram

if TYPE_CHECKING:
    from app.graph.state import ScreenerState

# Terminal `current_step` values a screening run can end on. Counted once per
# run in `record_node_metrics` — the parse/critic loop's intermediate steps
# ("parsing", "critiquing", "awaiting_approval") are deliberately excluded.
TERMINAL_OUTCOMES = frozenset({"done", "failed", "escalated"})

# The subset of terminal outcomes where the parse/critic loop actually resolved,
# so `state["parse_attempts"]` is a meaningful loop-depth count. A "failed" run
# (router-rejected input or a Parser LLM outage) never converged the loop, so its
# attempt count would just pollute the parse_attempts distribution.
_LOOP_RESOLVED_OUTCOMES = frozenset({"done", "escalated"})

# Latency buckets tuned for LLM-bound work: a fast local extraction lands near
# the low end, a slow hosted call or a retry storm rides the long tail. Shared
# by the two duration histograms so their p95s are comparable at a glance.
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0)

screenings_total = Counter(
    "screenings_total",
    "Screening runs that reached a terminal outcome, by outcome.",
    ["outcome"],
)

agent_node_duration_seconds = Histogram(
    "agent_node_duration_seconds",
    "Wall-clock duration of a single agent node execution.",
    ["agent"],
    buckets=_LATENCY_BUCKETS,
)

critic_rejections_total = Counter(
    "critic_rejections_total",
    "Blocking Critic findings, by the rule that fired (LLM-SEM for semantic review).",
    ["rule_id"],
)

parse_attempts = Histogram(
    "parse_attempts",
    "Parser attempts a screening needed before the Critic loop resolved.",
    # Small-integer buckets: MAX_PARSE_ATTEMPTS defaults to 3 and caps at 10.
    buckets=(1, 2, 3, 4, 5, 10),
)

llm_call_duration_seconds = Histogram(
    "llm_call_duration_seconds",
    "Duration of one logical LLM call (all retries folded in), by provider.",
    ["provider"],
    buckets=_LATENCY_BUCKETS,
)

llm_call_failures_total = Counter(
    "llm_call_failures_total",
    "LLM calls that ultimately failed after exhausting retries, by provider.",
    ["provider"],
)


def record_node_metrics(node: str, state: ScreenerState, result: dict, duration_s: float) -> None:
    """Record every node-level metric for one node execution.

    Called from the graph's `_instrument` decorator so agent bodies stay free of
    metrics plumbing. Always records the node's duration; additionally counts a
    terminal outcome when the node ended the run, and — only for a run whose
    parse/critic loop actually resolved (done/escalated) — the attempt depth that
    produced it. Failed runs are excluded from parse_attempts: their count
    reflects an abort, not loop depth, and would skew the distribution.
    """
    agent_node_duration_seconds.labels(agent=node).observe(duration_s)
    outcome = result.get("current_step")
    if outcome in TERMINAL_OUTCOMES:
        screenings_total.labels(outcome=outcome).inc()
        if outcome in _LOOP_RESOLVED_OUTCOMES:
            parse_attempts.observe(state.get("parse_attempts", 0))
