"""Prometheus metric definitions — the single home for every custom metric.

Standard HTTP metrics (request count, latency, in-flight) are added by
`prometheus-fastapi-instrumentator` in `app/main.py`; this module owns the
*domain* metrics that answer the questions plain HTTP timings can't: p95
screening duration, how often the Critic rejects and on which rule, how deep
the self-correction loop runs, LLM call latency/failures per provider, and what
the models cost in tokens and money (#101).

Everything registers against prometheus_client's default registry, so defining
each metric exactly once at import time is what keeps a re-import from raising a
duplicate-registration error. Nothing else in the codebase constructs a metric
— nodes and services call the objects (or the helpers) declared here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from prometheus_client import Counter, Histogram

from app.services import usage

if TYPE_CHECKING:
    from app.graph.state import ScreenerState

# The epoch every counter below covers. They are created at import — once per
# process, before the first request — and prometheus_client holds their values in
# memory, so a restart resets them to zero. Anything that *reports* these numbers
# has to say which window they describe (see services/metrics_summary.py, #58);
# Prometheus itself works this out from the `_created` samples in the exposition.
COUNTERS_SINCE = datetime.now(UTC)

# Terminal `current_step` values a screening run can end on. Counted once per
# run in `record_node_metrics` — the parse/critic loop's intermediate steps
# ("parsing", "critiquing", "awaiting_approval") are deliberately excluded.
#
# "rejected" (#91) is the one outcome no node produces: a reviewer stops the run
# from outside the graph, so it is counted by `record_rejection` instead. It
# belongs in this set all the same — this is what the funnel enumerates, and an
# outcome missing from it would make the funnel's total disagree with the runs.
TERMINAL_OUTCOMES = frozenset({"done", "failed", "escalated", "rejected"})

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

notifications_total = Counter(
    "notifications_total",
    "Gate/escalation notifications attempted, by channel and outcome (#60).",
    ["channel", "outcome"],
)

# Cost buckets in USD, spanning a local run (exactly 0, which lands in the first
# bucket) through a hosted screening of a long protocol against a large cohort.
# Sub-cent resolution at the low end is what makes the median readable on an
# instance whose screenings cost fractions of a cent; the long tail is there so a
# runaway parse/critic loop shows up as a tail rather than as a clipped bar.
#
# The middle is deliberately dense. A quantile read off a histogram interpolates
# within whichever bucket the rank lands in, so bucket width *is* the error bar
# on the figure this whole feature exists to report — a coarse 0.05→0.1 step made
# a measured $0.054 screening read as $0.075. Roughly-halving steps through the
# cents range keep that error inside ~20% of the value, which is the resolution a
# "what does a screening cost" figure has to have to be worth printing.
_COST_BUCKETS = (
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.0075,
    0.01,
    0.015,
    0.02,
    0.03,
    0.04,
    0.06,
    0.08,
    0.1,
    0.15,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Tokens consumed by LLM calls, by node, provider and kind (prompt/completion).",
    ["node", "provider", "kind"],
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Estimated USD spent on LLM calls, by node and provider (0 for unpriced models).",
    ["node", "provider"],
)

screening_cost_usd = Histogram(
    "screening_cost_usd",
    "Estimated USD one screening's LLM calls cost, observed once per terminal run.",
    buckets=_COST_BUCKETS,
)

screening_node_cost_usd = Histogram(
    "screening_node_cost_usd",
    "One node's share of one screening's estimated USD cost, by node.",
    ["node"],
    buckets=_COST_BUCKETS,
)

term_mapping_resolutions_total = Counter(
    "term_mapping_resolutions_total",
    "Criterion/term resolutions the cohort required — what a per-patient matcher would ask.",
)

term_mapping_llm_pairs_total = Counter(
    "term_mapping_llm_pairs_total",
    "Distinct criterion/term pairs actually sent to the LLM — the cache's misses.",
)


def record_llm_call(
    provider: str, model: str, tokens: usage.TokenUsage, cost_micro_usd: int
) -> None:
    """Count one completed LLM call's tokens and money.

    Called from `services/llm.py` the moment a call resolves, so the counters
    reflect every call the instance made — including calls in a node run that then
    raised, whose state update LangGraph discards. That is deliberate: the tokens
    were spent either way, and a cost counter that under-reported failures would
    make a retry storm look cheap.

    The node label comes from `usage.current_node()` — the scope `graph/builder.py`
    opens around each node body — so it is the same name
    `agent_node_duration_seconds` uses and cannot drift from it.
    """
    node = usage.current_node()
    llm_tokens_total.labels(node=node, provider=provider, kind="prompt").inc(tokens.prompt_tokens)
    llm_tokens_total.labels(node=node, provider=provider, kind="completion").inc(
        tokens.completion_tokens
    )
    llm_cost_usd_total.labels(node=node, provider=provider).inc(usage.usd(cost_micro_usd))


def record_term_mapping(resolutions: int, llm_pairs: int) -> None:
    """Count one screening's term-mapping work: what the cohort needed against what
    the LLM was actually asked (#101).

    The two together are the caching claim as a number — see
    `graph/nodes/matcher.build_verdict_cache`, which owns both definitions.
    """
    term_mapping_resolutions_total.inc(resolutions)
    term_mapping_llm_pairs_total.inc(llm_pairs)


def record_screening_usage(calls: Iterable[Mapping[str, Any]]) -> None:
    """Observe one finished run's LLM bill, in total and per node (#101).

    Observed at the frame that ended the run, so `screening_cost_usd`'s
    denominator is runs rather than calls and its median is "what a screening
    costs" rather than "what a call costs".

    **A run that made no LLM call is not observed at all.** The Router rejects
    non-protocol input before the Parser ever runs, and those rejections are
    terminal — so on an instance that turns away a few uploads, observing their
    zeros would pull the median toward a figure no *screening* ever cost. (A run
    whose calls were merely unpriced is a different case and *is* observed: its
    cost really is zero, and on a local deployment that zero is the honest
    median.) The per-node histogram applies the same rule for the same reason: a
    run parked before the Matcher must not contribute a zero to the Matcher's
    distribution.

    One caveat this shares with `screenings_total`: it records terminal *frames*,
    and a resumable graph lets one run reach more than one. A run that escalated
    and was then edited and re-run (#53) is observed twice — once with what it had
    spent at the escalation, once with its final total. The funnel counts that run
    in two bars for the same reason, and the same reading applies here: these are
    the terminal states a run passed through.
    """
    run = usage.summarize(calls)
    if not run["calls"]:
        return
    screening_cost_usd.observe(run["cost_usd"])
    for node in run["nodes"]:
        screening_node_cost_usd.labels(node=node["node"]).observe(node["cost_usd"])


def record_node_metrics(node: str, state: ScreenerState, result: dict, duration_s: float) -> None:
    """Record every node-level metric for one node execution.

    Called from the graph's `_instrument` decorator so agent bodies stay free of
    metrics plumbing. Always records the node's duration; additionally counts a
    terminal outcome when the node ended the run, and — only for a run whose
    parse/critic loop actually resolved (done/escalated) — the attempt depth that
    produced it. Failed runs are excluded from parse_attempts: their count
    reflects an abort, not loop depth, and would skew the distribution.

    A terminal frame is also where the run's LLM bill is observed (#101). The
    calls are the state's (everything the run spent before this node) plus this
    node's own, which `_instrument` has already attached to `result` — the same
    two halves the reducer is about to merge into the checkpoint, read one beat
    early so the histogram and the stored figure describe the same run.
    """
    agent_node_duration_seconds.labels(agent=node).observe(duration_s)
    outcome = result.get("current_step")
    if outcome in TERMINAL_OUTCOMES:
        screenings_total.labels(outcome=outcome).inc()
        if outcome in _LOOP_RESOLVED_OUTCOMES:
            parse_attempts.observe(state.get("parse_attempts", 0))
        record_screening_usage([*usage.calls_of(state), *usage.calls_of(result)])


def record_rejection(values: Mapping[str, Any]) -> None:
    """Count a run a reviewer stopped at the human gate (#91).

    The counterpart to `record_node_metrics` for the one terminal outcome that
    happens outside the graph: no node runs when a screening is rejected, so
    nothing would otherwise increment the funnel. Called once per rejection, from
    the same service call that writes the decision into the checkpoint — and only
    after that write succeeds, so a counted rejection is always a durable one.

    `parse_attempts` is deliberately not observed: like a failure, a rejection
    says nothing about how deep the parse/critic loop ran, and recording the
    count at the moment a human intervened would skew the distribution.

    A run rejected *after* escalating lands in both bars, exactly as a run edited
    and re-run to completion already lands in `escalated` and `done`: this counter
    records the terminal states a run passed through, and a resumable graph lets
    one run pass through more than one.

    `values` is the run's checkpoint at the moment of rejection, so its LLM bill
    joins the cost distribution (#101) the same way a run that finished through a
    node does. A rejected protocol still cost a parse and a Critic pass, and a
    median that omitted those would understate what screening actually spends.
    """
    screenings_total.labels(outcome="rejected").inc()
    record_screening_usage(usage.calls_of(values))
