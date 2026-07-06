"""Model client factory — swap the engine, keep the graph.

Set LLM_PROVIDER=ollama (default) for local Llama via Ollama, or
LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY for hosted Claude.
"""

import os
from functools import lru_cache

from langchain_core.language_models import BaseChatModel


@lru_cache(maxsize=1)
def get_llm(temperature: float = 0.0) -> BaseChatModel:
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    llm: BaseChatModel
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(model="claude-sonnet-5", temperature=temperature)
    else:
        from langchain_ollama import ChatOllama

        llm = ChatOllama(
            model=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
            temperature=temperature,
        )
    return llm
