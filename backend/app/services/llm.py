"""Model client factory — swap the engine, keep the graph.

All provider/model selection comes from Settings (see app/config.py):
LLM_PROVIDER=ollama (default) for local Llama via Ollama, or
LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY for hosted Claude.

`invoke_with_retry` is the one door every LLM call goes through: transient
failures (connection, timeout, 429/5xx) get exponential backoff with jitter;
anything else — above all schema-validation errors — propagates immediately
so a deterministic failure is never retried. Being the one door is also what
makes it the place token and cost accounting attaches (#101) — see
`_UsageRecorder` and `services/usage.py`.
"""

import time
from functools import lru_cache
from typing import Any

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings
from app.exceptions import LLMUnavailableError
from app.logging_config import get_logger
from app.services import metrics, usage
from app.services.metrics import llm_call_duration_seconds, llm_call_failures_total

log = get_logger("llm")

MAX_LLM_ATTEMPTS = 3

# Module-level so tests can swap in wait_none() and provider quirks stay in one place.
_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=8.0)


def is_transient(exc: BaseException) -> bool:
    """Worth retrying: network/timeout failures and 429/5xx provider responses.

    Provider SDK errors (anthropic.APIStatusError, ollama.ResponseError) all
    expose `status_code`, so we duck-type instead of importing both SDKs.
    """
    if isinstance(exc, ConnectionError | TimeoutError | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status: int | None = exc.response.status_code
    else:
        status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return False


class _UsageRecorder(BaseCallbackHandler):
    """Collects the token usage a provider reports, across every retry attempt.

    A callback rather than a read of the return value, because every call site
    goes through `with_structured_output(...)`, whose runnable yields a validated
    Pydantic model — the `AIMessage` carrying `usage_metadata` never reaches the
    caller. The callback sits under the parser and sees the raw generation.

    Attempts accumulate rather than replace: a call that failed twice and
    succeeded on the third try consumed tokens three times, and the provider
    billed for all three. `model` keeps the *last* model reported, which on a
    retry is the one that actually produced the answer.
    """

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model: str | None = None
        self.reported = False

    def on_llm_end(self, response: LLMResult, **_kwargs: Any) -> None:
        for generation in (gen for batch in response.generations for gen in batch):
            message = getattr(generation, "message", None)
            metadata = getattr(message, "usage_metadata", None)
            if not isinstance(metadata, dict):
                continue
            prompt = metadata.get("input_tokens")
            completion = metadata.get("output_tokens")
            if not isinstance(prompt, int) or not isinstance(completion, int):
                continue
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.reported = True
            served = getattr(message, "response_metadata", None)
            if isinstance(served, dict):
                name = served.get("model_name") or served.get("model")
                if isinstance(name, str) and name:
                    self.model = name

    def usage(self, input_: Any, result: Any) -> usage.TokenUsage:
        """What the call consumed — reported if the provider said, estimated if not.

        The estimate reads the characters that crossed the boundary in each
        direction. It exists for the stub provider (#10), which does no inference
        and therefore reports nothing: AC 2 of #101 asks it to show a non-zero
        token count at zero cost, because "free" must not read as "no work".
        """
        if self.reported:
            return usage.TokenUsage(self.prompt_tokens, self.completion_tokens)
        return usage.TokenUsage(
            usage.estimate_tokens(str(input_)),
            usage.estimate_tokens(str(result)),
            estimated=True,
        )


def invoke_with_retry(runnable: Runnable, input_: Any) -> Any:
    """Invoke `runnable` with backoff on transient errors, accounting for what it cost.

    Raises LLMUnavailableError once MAX_LLM_ATTEMPTS transient failures are
    exhausted; non-transient errors (validation, bad request) raise on the
    first occurrence, untouched.

    Tokens and cost are recorded only for a call that returned something (#101).
    A call that raised has no result to measure and, for the transient case, no
    usable answer to attribute a price to — its cost shows up where it belongs,
    in `llm_call_failures_total` and the duration histogram, both of which still
    cover the failed span.
    """
    retryer = Retrying(
        stop=stop_after_attempt(MAX_LLM_ATTEMPTS),
        wait=_RETRY_WAIT,
        retry=retry_if_exception(is_transient),
        reraise=True,
    )
    # One observation per logical call (retries folded into the span) so the
    # duration histogram and failure counter share a denominator per provider.
    settings = get_settings()
    provider = settings.llm_provider
    recorder = _UsageRecorder()
    started = time.perf_counter()
    try:
        result = retryer(runnable.invoke, input_, config={"callbacks": [recorder]})
    except Exception as exc:
        # Count only genuine backend failures (transient errors that exhausted
        # retries) — a non-transient error means the backend *answered* and the
        # output was unusable (schema violation, bad request). Lumping those in
        # would turn this counter into false backend-outage alerts.
        if is_transient(exc):
            llm_call_failures_total.labels(provider=provider).inc()
            raise LLMUnavailableError(
                f"LLM backend unavailable after {MAX_LLM_ATTEMPTS} attempts: {exc}"
            ) from exc
        raise
    finally:
        llm_call_duration_seconds.labels(provider=provider).observe(time.perf_counter() - started)
    # Only reached on success — every path through `except` re-raises. Outside the
    # `finally` so the duration histogram times the model call and not the
    # bookkeeping that follows it.
    _record_usage(provider, recorder, input_, result)
    return result


def _record_usage(provider: str, recorder: _UsageRecorder, input_: Any, result: Any) -> None:
    """Price one completed call and file it against the node that made it.

    Accounting must never fail a screening: a bad `LLM_PRICES` row or a provider
    returning something unexpected costs the run's cost figure, not the run. The
    tokens are the model's; the price is configuration; the node comes from the
    scope `graph/builder.py` opened — see `services/usage.py`.
    """
    try:
        tokens = recorder.usage(input_, result)
        model = recorder.model or configured_model(provider)
        price = usage.price_of(model, parse_prices(get_settings().llm_prices))
        micros = usage.cost_micro_usd(price, tokens)
        metrics.record_llm_call(provider, model, tokens, micros)
        usage.record(
            usage.LlmCall(
                node=usage.current_node(),
                provider=provider,
                model=model,
                prompt_tokens=tokens.prompt_tokens,
                completion_tokens=tokens.completion_tokens,
                cost_micro_usd=micros,
                estimated=tokens.estimated,
            )
        )
    except Exception:  # noqa: BLE001 — cost accounting must not break a screening
        log.warning("llm.usage_accounting_failed", provider=provider, exc_info=True)


def configured_model(provider: str) -> str:
    """The model this provider is configured to use — the fallback label for a
    backend that does not name the model it served."""
    settings = get_settings()
    if provider == "anthropic":
        return settings.anthropic_model
    if provider == "ollama":
        return settings.ollama_model
    return provider


@lru_cache(maxsize=8)
def parse_prices(spec: str) -> dict[str, usage.ModelPrice]:
    """`LLM_PRICES` parsed, memoized on the raw string.

    Parsed per call site would re-split the same setting on every LLM call;
    keyed on the spec (rather than cached with no key) so a test that swaps the
    setting sees its own table rather than the first one parsed in the process.
    """
    return usage.parse_prices(spec)


@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    settings = get_settings()
    llm: BaseChatModel
    if settings.llm_provider == "stub":
        # Load-test / offline mode: no inference, deterministic timing (#10).
        from app.services.stub_llm import StubChatModel

        llm = StubChatModel(latency_seconds=settings.stub_latency_seconds)
    elif settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(
            model=settings.anthropic_model,
            temperature=settings.llm_temperature,
            api_key=settings.anthropic_api_key,
        )
    else:
        from langchain_ollama import ChatOllama

        llm = ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=settings.llm_temperature,
            # Cap generation so a degenerate loop can't run unbounded and hang
            # the screening (see Settings.ollama_num_predict).
            num_predict=settings.ollama_num_predict,
        )
    return llm
