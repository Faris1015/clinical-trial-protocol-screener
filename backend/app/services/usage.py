"""Token and cost accounting for every LLM call (#101).

`services/metrics.py` counts screenings, node duration, Critic rejections, parse
attempts, LLM latency and failures. None of them count **tokens** or **money** —
so the project's central architectural claim (LLMs only where language
understanding is required; deterministic checks everywhere else; term mappings
resolved once per screening rather than once per patient) was an assertion
nobody could check. A cost-per-screening figure is what turns it into a
measurement.

Four decisions worth knowing before editing this module:

**One ledger per node run, opened by the graph.** `services/llm.py` records a
call the moment it completes; `graph/builder.py`'s `_instrument` decorator is
what says *which node* that call belongs to, by opening a `collecting(node)`
scope around the node body. Node bodies stay free of accounting plumbing, and
the label can never drift from the node the graph actually ran — it is the same
name `agent_node_duration_seconds` is labelled with. The scope is a
`ContextVar`, set and read on one thread inside one call stack (LangGraph runs a
sync node in a worker thread, and nothing inside a node awaits), so there is no
cross-task leakage to reason about.

**Prices are configuration, and an unpriced model is free.** `LLM_PRICES` maps a
model to its USD-per-million-tokens pair (see `Settings.llm_prices`). A model
with no entry — every Ollama model, and the stub — costs 0.0 while still
reporting the tokens it consumed, which is exactly the reading a local
deployment needs: *this work is not free of compute, it is free of invoice*.
Reporting no tokens instead would hide the workload; reporting a guessed price
would invent an invoice.

**Providers that report usage are believed; the rest are estimated, and say so.**
Anthropic and Ollama both return `usage_metadata` on the message, and that is
what gets recorded. The stub does no inference at all and reports nothing, so its
tokens are estimated from the characters that crossed the boundary (see
`estimate_tokens`). Every call carries `estimated`, and every rollup carries how
many of its calls were — a cost figure that silently mixed measured and guessed
tokens would be the one way this module could mislead.

**Costs are carried as integer micro-USD.** A screening costs cents; summing
floats across a cohort's worth of calls accumulates representation error, and the
runs index stores the figure in an INTEGER column. `micro_usd` is the one
conversion, `usd` its inverse, and every reported dollar figure comes from that
pair rather than from arithmetic at the point of rendering.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NamedTuple, TypedDict

# The nodes that can make an LLM call, in pipeline order. The router is purely
# deterministic and `human_escalation` writes an event — neither touches a model,
# so neither appears here or in the per-node breakdown. A call recorded under any
# other name is still counted (the totals are summed over what was recorded, not
# over this tuple); it simply sorts last, so a future LLM-bound node shows up
# rather than silently vanishing from the split.
LLM_NODES = ("parser", "critic", "matcher")

# Characters per token for the estimator. English prose runs ~4 chars/token on
# every current tokenizer, and the estimate exists only for backends that report
# no usage at all (the stub). Deliberately crude, and always flagged `estimated`
# — a more elaborate approximation would still be an approximation, and would
# invite being read as a measurement.
CHARS_PER_TOKEN = 4


class TokenUsage(NamedTuple):
    """Prompt and completion tokens for one logical LLM call.

    `estimated` is True when the provider reported nothing and the counts were
    derived from character length instead. It rides with the numbers rather than
    being inferred from the provider, because a provider that reports usage on
    most calls and not on one would otherwise be silently mixed.
    """

    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LlmCall(TypedDict):
    """One LLM call, as it is written into the run's checkpoint.

    Appended to `ScreenerState["llm_usage"]` through the same `operator.add`
    reducer the event log uses, so the parse/critic loop's repeated calls
    accumulate rather than overwrite. That makes the record durable across the
    human gate — a screening's cost spans two HTTP requests (the stream that
    parks it, the approval that resumes it) and only the checkpoint bridges them.

    `cost_micro_usd` rather than a float: see the module docstring. `model` is
    the model the provider actually served, when it says so, and the configured
    model otherwise.
    """

    node: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int
    estimated: bool


class NodeUsage(TypedDict):
    """One node's share of a run: what it spent and how many calls it took."""

    node: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    tokens: int
    cost_micro_usd: int
    cost_usd: float


class RunUsage(TypedDict):
    """A run's whole LLM bill, and what it is made of.

    `calls` is every LLM call the run made — the figure the matcher's caching
    claim is pinned against (see `tests/test_matcher_semantic.py`). `nodes` is
    the same total split by the node that spent it, in pipeline order, and it
    omits nodes that made no call: a run parked at the gate has no matcher row,
    which is the honest shape for "this has not happened yet" rather than a zero
    that reads as "this was free".

    `estimated_calls` is how many of `calls` had their tokens estimated rather
    than reported. `priced` is whether any model in the run had a price at all —
    False on a local-only instance, which is what lets a view say "no cost" instead
    of "$0.00", two different claims.
    """

    calls: int
    prompt_tokens: int
    completion_tokens: int
    tokens: int
    cost_micro_usd: int
    cost_usd: float
    estimated_calls: int
    priced: bool
    nodes: list[NodeUsage]


class ModelPrice(NamedTuple):
    """USD per million tokens, input and output."""

    input_per_mtok: float
    output_per_mtok: float


# --- Money ------------------------------------------------------------------

# One million tokens, and one million micro-dollars. Named because both appear
# in the same expression and reading `1_000_000 / 1_000_000` back is a coin flip.
_TOKENS_PER_MTOK = 1_000_000
_MICRO_PER_USD = 1_000_000


def usd(cost_micro_usd: int) -> float:
    """Micro-USD as dollars, to six decimals.

    The one place the stored integer becomes a dollar figure. Six decimals is the
    full precision of the micro-dollar it came from, so this is a change of unit
    rather than a rounding — views round further for display, from this value.
    """
    return round(cost_micro_usd / _MICRO_PER_USD, 6)


def cost_micro_usd(price: ModelPrice | None, usage: TokenUsage) -> int:
    """What one call cost, in micro-USD, at the given price.

    An unpriced model costs 0 — see the module docstring. Rounded (not truncated)
    to the nearest micro-dollar so a cohort of small calls does not drift
    systematically low.
    """
    if price is None:
        return 0
    dollars = (
        usage.prompt_tokens * price.input_per_mtok + usage.completion_tokens * price.output_per_mtok
    ) / _TOKENS_PER_MTOK
    return round(dollars * _MICRO_PER_USD)


def parse_prices(spec: str) -> dict[str, ModelPrice]:
    """Parse `LLM_PRICES` into a model → price table.

    The format is `model=input/output` per entry, comma- or newline-separated,
    with the two figures in USD per million tokens (`claude-sonnet-5=3/15`). The
    separator is `=` and not `:` because a model id may contain a colon —
    Ollama's are all `name:tag` — and splitting on it would make `qwen2.5:7b`
    unaddressable.

    A malformed entry is skipped rather than raised on: this is read at request
    time by the cost accountant, and a typo in one row must not take down a
    screening. The rows that parsed still price their models; the one that did not
    leaves its model unpriced, which reports as zero cost and non-zero tokens —
    visibly wrong on the metrics page rather than invisibly wrong in a total.
    """
    table: dict[str, ModelPrice] = {}
    for raw in spec.replace("\n", ",").split(","):
        entry = raw.strip()
        if not entry:
            continue
        model, _, rates = entry.partition("=")
        input_rate, _, output_rate = rates.partition("/")
        try:
            price = ModelPrice(float(input_rate), float(output_rate))
        except ValueError:
            continue
        if model.strip() and price.input_per_mtok >= 0 and price.output_per_mtok >= 0:
            table[model.strip()] = price
    return table


def price_of(model: str, table: Mapping[str, ModelPrice]) -> ModelPrice | None:
    """The price for `model`, or None when the table does not name it.

    An exact match wins; otherwise the longest configured id that `model` starts
    with does, so a dated snapshot (`claude-sonnet-5-20260101`) is priced by its
    family entry (`claude-sonnet-5`) without every snapshot needing a row. Longest
    rather than first, so a more specific entry always beats a shorter prefix of
    itself.
    """
    exact = table.get(model)
    if exact is not None:
        return exact
    prefixes = [key for key in table if model.startswith(key)]
    if not prefixes:
        return None
    return table[max(prefixes, key=len)]


def estimate_tokens(text: str) -> int:
    """Tokens a string is worth, approximately — the fallback for a backend that
    reports no usage.

    Never zero for non-empty text: a call that happened consumed *something*, and
    a zero would make the stub read as having made no call at all, which is the
    one thing AC 2 exists to prevent.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


# --- The per-node ledger ----------------------------------------------------

# The node whose body is currently running, and the list its calls append to.
# Both are set by `graph/builder.py`'s `_instrument` wrapper and read by
# `services/llm.py`. A call made outside any node scope (a direct unit test, a
# service that calls the model itself) still records its metrics — it just has no
# state to be written into, and is labelled with `UNATTRIBUTED_NODE` so it is
# findable rather than mislabelled as one of the real nodes.
UNATTRIBUTED_NODE = "unattributed"

_node: ContextVar[str] = ContextVar("llm_usage_node", default=UNATTRIBUTED_NODE)
_ledger: ContextVar[list[LlmCall] | None] = ContextVar("llm_usage_ledger", default=None)


@contextmanager
def collecting(node: str) -> Iterator[list[LlmCall]]:
    """Attribute every LLM call made inside this block to `node`.

    Yields the list the calls land in, so the caller can attach it to the node's
    state update. Restores the previous scope on the way out — including on an
    exception, so a node that raises mid-run cannot leave its name bound for
    whatever runs next.
    """
    calls: list[LlmCall] = []
    node_token = _node.set(node)
    ledger_token = _ledger.set(calls)
    try:
        yield calls
    finally:
        _ledger.reset(ledger_token)
        _node.reset(node_token)


def current_node() -> str:
    """The node an LLM call made right now belongs to."""
    return _node.get()


def record(call: LlmCall) -> None:
    """Append a completed call to the open ledger, if there is one."""
    calls = _ledger.get()
    if calls is not None:
        calls.append(call)


# --- Rollups ----------------------------------------------------------------


def _node_order(node: str) -> tuple[int, str]:
    """Sort key: the known LLM nodes in pipeline order, anything else after."""
    return (LLM_NODES.index(node), node) if node in LLM_NODES else (len(LLM_NODES), node)


def calls_of(values: Mapping[str, Any]) -> list[LlmCall]:
    """The `llm_usage` entries of a checkpoint, defensively.

    A checkpoint hand-edited (or written by a build that predates #101) can carry
    anything here; a malformed entry is dropped rather than crashing the run
    detail view it is rendered on. Public because the metrics recorder and the
    run payload read the same field and must agree on what counts as an entry.
    """
    value = values.get("llm_usage")
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]  # type: ignore[misc]


def _int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize(calls: Iterable[Mapping[str, Any]]) -> RunUsage:
    """Reduce a run's LLM calls to what it spent, in total and per node.

    Pure and total: an empty run reduces to an all-zero payload with no node rows,
    which is the shape a run that has not reached the Parser yet has, and what
    tells the views to render nothing rather than "$0.00".
    """
    rows = list(calls)
    per_node: dict[str, list[int]] = {}
    for row in rows:
        node = str(row.get("node") or UNATTRIBUTED_NODE)
        totals = per_node.setdefault(node, [0, 0, 0, 0])
        totals[0] += 1
        totals[1] += _int(row, "prompt_tokens")
        totals[2] += _int(row, "completion_tokens")
        totals[3] += _int(row, "cost_micro_usd")

    nodes = [
        NodeUsage(
            node=node,
            calls=calls_made,
            prompt_tokens=prompt,
            completion_tokens=completion,
            tokens=prompt + completion,
            cost_micro_usd=micros,
            cost_usd=usd(micros),
        )
        for node, (calls_made, prompt, completion, micros) in sorted(
            per_node.items(), key=lambda item: _node_order(item[0])
        )
    ]

    prompt_tokens = sum(row["prompt_tokens"] for row in nodes)
    completion_tokens = sum(row["completion_tokens"] for row in nodes)
    micros = sum(row["cost_micro_usd"] for row in nodes)
    return RunUsage(
        calls=len(rows),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        tokens=prompt_tokens + completion_tokens,
        cost_micro_usd=micros,
        cost_usd=usd(micros),
        estimated_calls=sum(1 for row in rows if row.get("estimated")),
        # Any priced call at all makes the run's figure a real (if small) amount
        # of money. A run of only unpriced calls has no cost to report — which is
        # a different statement from "it cost nothing", and the views say so.
        priced=any(_int(row, "cost_micro_usd") > 0 for row in rows),
        nodes=nodes,
    )


def build_usage(values: Mapping[str, Any]) -> RunUsage:
    """A run's LLM bill, read off its checkpoint (#101).

    `values` is the checkpoint block of `GET /api/screenings/{id}/state` — the
    same argument `coverage.build_coverage` and `attrition.build_attrition` take,
    so a caller holding one payload derives all of them without unpacking any.
    Only `llm_usage` is read.
    """
    return summarize(calls_of(values))
