"""Model client factory — swap the engine, keep the graph.

Set LLM_PROVIDER=ollama (default) for local Llama via Ollama, or
LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY for hosted Claude.
"""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def get_llm(temperature: float = 0.0):
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model="claude-sonnet-5", temperature=temperature)
    from langchain_ollama import ChatOllama
    return ChatOllama(
        model=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        temperature=temperature,
    )
