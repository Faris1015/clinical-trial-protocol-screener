"""Token and cost accounting (#101) — the arithmetic, the ledger, and the wiring.

Four halves, roughly. The price table and the rollup are pure functions with
exact answers, so they are asserted exactly. The ledger is a contextvar the graph
opens and `services/llm.py` writes into, so it is tested through
`invoke_with_retry` against a fake runnable rather than by poking the contextvar
— the thing worth pinning is that a call made inside a node scope lands under
that node's name, which is the whole of AC 1.

The last part is the claim that motivates the issue: an unpriced backend (Ollama,
the load-test stub) must report a *non-zero token count at zero cost*. "Free of
invoice" and "did no work" are different statements, and only one of them is true
of a local model — a test is the only thing that keeps a later refactor from
collapsing them.
"""

from typing import Any, cast

import pytest
from langchain_core.runnables import Runnable
from prometheus_client import CollectorRegistry, Counter
from tenacity import wait_none

import app.services.llm as llm_service
from app.services import usage
from app.services.llm import invoke_with_retry
from app.services.usage import ModelPrice, TokenUsage


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(llm_service, "_RETRY_WAIT", wait_none())


@pytest.fixture(autouse=True)
def isolated_counters(monkeypatch):
    """Private copies of the token/cost counters.

    `record_llm_call` increments process-global counters; the assertions here are
    about the ledger and the arithmetic, and a suite-wide registry would make a
    second run of this module double every figure.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(
        llm_service.metrics,
        "llm_tokens_total",
        Counter("llm_tokens_total", "test", ["node", "provider", "kind"], registry=registry),
    )
    monkeypatch.setattr(
        llm_service.metrics,
        "llm_cost_usd_total",
        Counter("llm_cost_usd_total", "test", ["node", "provider"], registry=registry),
    )


class _Runnable:
    """A runnable that returns `result` and reports no usage.

    Reporting nothing is the interesting case: it is what the stub provider does,
    so this double exercises the estimator rather than the happy path.
    """

    def __init__(self, result: object = "answer") -> None:
        self.result = result
        self.configs: list[Any] = []

    def invoke(self, _input: object, config: object = None, **_kwargs: object) -> object:
        self.configs.append(config)
        return self.result


class _ReportingRunnable(_Runnable):
    """A runnable whose callbacks receive a provider-reported usage payload.

    Mirrors what `langchain_anthropic` and `langchain_ollama` do: the raw
    `AIMessage` carries `usage_metadata`, and the structured-output parser above
    it swallows the message — which is exactly why the recorder is a callback.
    """

    def __init__(self, prompt: int, completion: int, model: str = "claude-sonnet-5") -> None:
        super().__init__()
        self.prompt = prompt
        self.completion = completion
        self.model = model

    def invoke(self, _input: object, config: object = None, **_kwargs: object) -> object:
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult

        message = AIMessage(
            content="",
            usage_metadata={
                "input_tokens": self.prompt,
                "output_tokens": self.completion,
                "total_tokens": self.prompt + self.completion,
            },
            response_metadata={"model_name": self.model},
        )
        result = LLMResult(generations=[[ChatGeneration(message=message)]])
        for handler in (config or {}).get("callbacks", []):  # type: ignore[union-attr]
            handler.on_llm_end(result)
        return self.result


# --- The price table ---------------------------------------------------------


def test_prices_parse_from_the_configured_string():
    table = usage.parse_prices("claude-sonnet-5=3/15, claude-haiku-4-5=1/5")
    assert table["claude-sonnet-5"] == ModelPrice(3.0, 15.0)
    assert table["claude-haiku-4-5"] == ModelPrice(1.0, 5.0)


def test_a_model_id_containing_a_colon_is_still_addressable():
    """Ollama ids are `name:tag`, which is why the separator is `=` and not `:`.

    Pinned because the obvious format (`model:in/out`) silently makes every local
    model unpriceable — and unpriceable reads as free, which is right for Ollama
    and would be wrong the day someone prices one.
    """
    table = usage.parse_prices("qwen2.5:7b=0.1/0.2")
    assert table["qwen2.5:7b"] == ModelPrice(0.1, 0.2)


def test_a_malformed_entry_is_skipped_rather_than_raised_on():
    """A typo in one row must not take down a screening — the rows that parsed
    still price their models, and the broken one leaves its model unpriced."""
    table = usage.parse_prices("claude-sonnet-5=3/15,garbage,also=bad/rows")
    assert set(table) == {"claude-sonnet-5"}


def test_a_dated_snapshot_inherits_its_family_price():
    """Longest-prefix matching, so every dated model id does not need its own row."""
    table = usage.parse_prices("claude-sonnet-5=3/15,claude-sonnet-5-mini=1/2")
    assert usage.price_of("claude-sonnet-5-20260101", table) == ModelPrice(3.0, 15.0)
    # The longer entry wins over a shorter prefix of itself.
    assert usage.price_of("claude-sonnet-5-mini-preview", table) == ModelPrice(1.0, 2.0)
    assert usage.price_of("qwen2.5:7b", table) is None


def test_cost_is_tokens_times_the_rate_in_micro_usd():
    price = ModelPrice(3.0, 15.0)  # USD per million tokens
    # 10k prompt at $3/Mtok = $0.03; 2k completion at $15/Mtok = $0.03. $0.06.
    micros = usage.cost_micro_usd(price, TokenUsage(10_000, 2_000))
    assert micros == 60_000
    assert usage.usd(micros) == 0.06


def test_an_unpriced_model_costs_nothing():
    assert usage.cost_micro_usd(None, TokenUsage(10_000, 2_000)) == 0


# --- The rollup --------------------------------------------------------------


def _call(node: str, prompt: int = 100, completion: int = 20, micros: int = 0, **over) -> dict:
    return {
        "node": node,
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cost_micro_usd": micros,
        "estimated": False,
        **over,
    }


def test_a_run_rolls_up_to_totals_and_a_per_node_split():
    run = usage.summarize(
        [
            _call("parser", micros=1_000),
            _call("parser", micros=2_000),
            _call("critic", micros=500),
            _call("matcher", prompt=50, completion=10, micros=250),
        ]
    )
    assert run["calls"] == 4
    assert run["tokens"] == 100 * 3 + 20 * 3 + 60
    assert run["cost_micro_usd"] == 3_750
    assert run["cost_usd"] == 0.00375
    assert [node["node"] for node in run["nodes"]] == ["parser", "critic", "matcher"]
    assert run["nodes"][0]["calls"] == 2
    assert run["nodes"][0]["cost_micro_usd"] == 3_000


def test_a_run_with_no_calls_rolls_up_to_nothing():
    """The shape a run that never reached the Parser has — complete, so no caller
    special-cases a null, and all-zero so the views render nothing at all."""
    run = usage.summarize([])
    assert run == {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tokens": 0,
        "cost_micro_usd": 0,
        "cost_usd": 0.0,
        "estimated_calls": 0,
        "priced": False,
        "nodes": [],
    }


def test_a_run_of_unpriced_calls_reports_tokens_but_is_not_priced():
    """AC 2: the local case. Real tokens, no money, and `priced` says which."""
    run = usage.summarize([_call("parser", micros=0), _call("critic", micros=0)])
    assert run["tokens"] > 0
    assert run["cost_usd"] == 0.0
    assert run["priced"] is False


def test_estimated_calls_are_counted_so_a_figure_can_say_it_is_estimated():
    run = usage.summarize([_call("parser"), _call("critic", estimated=True)])
    assert run["estimated_calls"] == 1


def test_a_malformed_checkpoint_entry_does_not_break_the_rollup():
    """A hand-edited checkpoint (or one from a build predating #101) must render
    the run detail page rather than 500 it."""
    assert usage.build_usage({})["calls"] == 0
    assert usage.build_usage({"llm_usage": "not a list"})["calls"] == 0
    assert usage.build_usage({"llm_usage": [None, _call("parser")]})["calls"] == 1


def test_a_call_from_an_unknown_node_still_counts_and_sorts_last():
    """A future LLM-bound node shows up rather than vanishing from the split."""
    run = usage.summarize([_call("auditor"), _call("parser")])
    assert [node["node"] for node in run["nodes"]] == ["parser", "auditor"]
    assert run["calls"] == 2


# --- The ledger, through the one door every call goes through ----------------


def test_a_call_is_attributed_to_the_node_scope_it_was_made_in(monkeypatch):
    """AC 1: tokens are labelled by node. The scope is opened by the graph's
    `_instrument` wrapper; this asserts the half `services/llm.py` owns."""
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings())
    runnable = _ReportingRunnable(prompt=1_000, completion=200)

    with usage.collecting("critic") as calls:
        invoke_with_retry(cast(Runnable, runnable), "input")

    assert len(calls) == 1
    assert calls[0]["node"] == "critic"
    assert calls[0]["prompt_tokens"] == 1_000
    assert calls[0]["completion_tokens"] == 200
    assert calls[0]["estimated"] is False
    # $3/Mtok on 1000 prompt + $15/Mtok on 200 completion = $0.006.
    assert calls[0]["cost_micro_usd"] == 6_000


def test_a_call_outside_any_node_scope_is_recorded_but_not_mislabelled(monkeypatch):
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings())
    invoke_with_retry(cast(Runnable, _ReportingRunnable(10, 5)), "input")
    # Nothing to append to, and nothing raised — the metrics still counted it.
    assert usage.current_node() == usage.UNATTRIBUTED_NODE


def test_the_scope_is_restored_even_when_the_node_raises():
    """A node that blows up must not leave its name bound for whatever runs next."""
    with pytest.raises(RuntimeError), usage.collecting("parser"):
        assert usage.current_node() == "parser"
        raise RuntimeError("boom")
    assert usage.current_node() == usage.UNATTRIBUTED_NODE


def test_retries_accumulate_tokens_because_every_attempt_was_billed(monkeypatch):
    """A call that failed twice and succeeded on the third consumed tokens three
    times, and the provider charged for all three."""
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings())

    class _FlakyReporter(_ReportingRunnable):
        def __init__(self) -> None:
            super().__init__(prompt=100, completion=10)
            self.attempts = 0

        def invoke(self, input_: object, config: object = None, **kwargs: object) -> object:
            self.attempts += 1
            result = super().invoke(input_, config, **kwargs)
            if self.attempts < 3:
                raise ConnectionError("refused")
            return result

    with usage.collecting("parser") as calls:
        invoke_with_retry(cast(Runnable, _FlakyReporter()), "input")

    assert calls[0]["prompt_tokens"] == 300
    assert calls[0]["completion_tokens"] == 30


def test_a_failed_call_records_no_usage(monkeypatch):
    """There is no result to measure, and no answer to attribute a price to. The
    failure is counted where it belongs — the failure counter and the duration
    histogram, both of which still cover the span."""
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings())

    class _Down:
        def invoke(self, _input: object, config: object = None, **_kwargs: object) -> object:
            raise ConnectionError("refused")

    with usage.collecting("parser") as calls:
        with pytest.raises(Exception, match="unavailable"):
            invoke_with_retry(cast(Runnable, _Down()), "input")

    assert calls == []


# --- AC 2: an unpriced backend reports tokens at zero cost -------------------


def test_a_backend_that_reports_no_usage_is_estimated_and_says_so(monkeypatch):
    """The stub does no inference, so nothing can report its tokens. Estimating
    from message length is what keeps its screenings from reading as no work at
    all — and `estimated` is what keeps the estimate from reading as a
    measurement."""
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings(provider="stub"))

    with usage.collecting("parser") as calls:
        invoke_with_retry(cast(Runnable, _Runnable("a canned extraction")), "a long prompt " * 20)

    assert calls[0]["estimated"] is True
    assert calls[0]["prompt_tokens"] > 0
    assert calls[0]["completion_tokens"] > 0
    # AC 2: non-zero tokens, zero cost — "stub" has no price-table entry.
    assert calls[0]["cost_micro_usd"] == 0


def test_ollama_reports_real_tokens_at_zero_cost(monkeypatch):
    """AC 2, the other half: a local model *does* report usage, so the tokens are
    measured rather than estimated — and still cost nothing, because a local
    model is free of invoice, not free of compute."""
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings(provider="ollama"))
    runnable = _ReportingRunnable(prompt=4_000, completion=600, model="qwen2.5:7b")

    with usage.collecting("parser") as calls:
        invoke_with_retry(cast(Runnable, runnable), "input")

    assert calls[0]["estimated"] is False
    assert calls[0]["prompt_tokens"] == 4_000
    assert calls[0]["cost_micro_usd"] == 0
    assert calls[0]["model"] == "qwen2.5:7b"


def test_accounting_never_fails_a_screening(monkeypatch):
    """A bad price table (or anything else going wrong in the bookkeeping) costs
    the run's cost figure, not the run."""
    monkeypatch.setattr(llm_service, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        llm_service, "parse_prices", lambda _spec: (_ for _ in ()).throw(ValueError("boom"))
    )

    with usage.collecting("parser") as calls:
        assert invoke_with_retry(cast(Runnable, _Runnable("ok")), "input") == "ok"

    assert calls == []


def _settings(provider: str = "anthropic"):
    """The handful of settings fields the accounting reads."""
    from types import SimpleNamespace

    return SimpleNamespace(
        llm_provider=provider,
        anthropic_model="claude-sonnet-5",
        ollama_model="qwen2.5:7b",
        llm_prices="claude-sonnet-5=3/15",
    )
