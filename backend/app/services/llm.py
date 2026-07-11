"""Model client factory — swap the engine, keep the graph.

All provider/model selection comes from Settings (see app/config.py):
LLM_PROVIDER=ollama (default) for local Llama via Ollama, or
LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY for hosted Claude.

`invoke_with_retry` is the one door every LLM call goes through: transient
failures (connection, timeout, 429/5xx) get exponential backoff with jitter;
anything else — above all schema-validation errors — propagates immediately
so a deterministic failure is never retried.
"""

from functools import lru_cache
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.config import get_settings
from app.exceptions import LLMUnavailableError

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


def invoke_with_retry(runnable: Runnable, input_: Any) -> Any:
    """Invoke `runnable` with backoff on transient errors.

    Raises LLMUnavailableError once MAX_LLM_ATTEMPTS transient failures are
    exhausted; non-transient errors (validation, bad request) raise on the
    first occurrence, untouched.
    """
    retryer = Retrying(
        stop=stop_after_attempt(MAX_LLM_ATTEMPTS),
        wait=_RETRY_WAIT,
        retry=retry_if_exception(is_transient),
        reraise=True,
    )
    try:
        return retryer(runnable.invoke, input_)
    except Exception as exc:
        if is_transient(exc):
            raise LLMUnavailableError(
                f"LLM backend unavailable after {MAX_LLM_ATTEMPTS} attempts: {exc}"
            ) from exc
        raise


@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    settings = get_settings()
    llm: BaseChatModel
    if settings.llm_provider == "anthropic":
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
        )
    return llm
