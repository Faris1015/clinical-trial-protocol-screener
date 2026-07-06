"""Model client factory — swap the engine, keep the graph.

All provider/model selection comes from Settings (see app/config.py):
LLM_PROVIDER=ollama (default) for local Llama via Ollama, or
LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY for hosted Claude.
"""

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import get_settings


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
