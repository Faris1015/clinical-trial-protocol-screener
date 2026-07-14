"""Liveness/readiness support for /health and /ready (#6).

Liveness ("is the process alive") is trivial and dependency-free — it lives in
the route itself. Readiness ("can this instance actually serve traffic") is the
interesting part: every dependency the request path needs must be reachable —
the LLM backend, the compliance rules, the patient EHR, and the checkpointer
database. Each check is bounded by its own timeout and they run concurrently,
so the whole probe stays well under a 2-second budget even when one dependency
hangs.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING

import httpx

from app.config import get_settings
from app.graph.nodes.critic import load_rules
from app.graph.nodes.matcher import load_patients
from app.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.persistence import ScreeningStore

log = get_logger("health")

# Per-check ceiling; checks run concurrently so the whole probe finishes within
# this bound (the issue's < 2s worst-case budget) even if one dependency hangs.
CHECK_TIMEOUT_S = 1.5


@lru_cache(maxsize=1)
def app_version() -> dict[str, str]:
    """Version + commit for the running build (see Settings.git_sha).

    Cached: the values are fixed for a process, and /health + /ready read them
    on every probe. Callers spread the result into a fresh dict, never mutate it.
    """
    try:
        version = pkg_version("protocol-screener")
    except PackageNotFoundError:  # pragma: no cover - only when not pip-installed
        version = "unknown"
    return {"version": version, "commit": get_settings().git_sha or "unknown"}


async def _check_llm() -> dict[str, object]:
    """Ping the configured LLM backend cheaply (no token spend)."""
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        url = "https://api.anthropic.com/v1/models"
        headers = {
            "x-api-key": settings.anthropic_api_key or "",
            "anthropic-version": "2023-06-01",
        }
    else:
        url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
        headers = {}
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
    return {"ok": True, "detail": f"{settings.llm_provider} backend reachable"}


async def _check_rules() -> dict[str, object]:
    """The compliance rules file parses and is a list (raises DataStoreError otherwise)."""
    rules = await asyncio.to_thread(load_rules)
    return {"ok": True, "detail": f"{len(rules)} compliance rules loaded"}


async def _check_patients() -> dict[str, object]:
    """The synthetic EHR is present and parseable."""
    patients = await asyncio.to_thread(load_patients)
    return {"ok": True, "detail": f"{len(patients)} patient records available"}


async def _check_db(store: ScreeningStore) -> dict[str, object]:
    """The checkpointer/store database answers a cheap query."""
    await store.exists("__ready_probe__")
    return {"ok": True, "detail": "screening store reachable"}


async def _run(name: str, check: Callable[[], Awaitable[dict[str, object]]]) -> tuple[str, dict]:
    try:
        return name, await asyncio.wait_for(check(), CHECK_TIMEOUT_S)
    except TimeoutError:
        log.warning("ready.check_timeout", check=name)
        return name, {"ok": False, "detail": f"timed out after {CHECK_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001 — a probe must convert any failure into a status
        log.warning("ready.check_failed", check=name, error=type(exc).__name__, detail=str(exc))
        return name, {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


async def readiness(store: ScreeningStore) -> tuple[bool, dict[str, dict]]:
    """Run every dependency check concurrently; return (all_ok, per-check results)."""
    checks: dict[str, Callable[[], Awaitable[dict[str, object]]]] = {
        "llm": _check_llm,
        "rules": _check_rules,
        "patients": _check_patients,
        "db": lambda: _check_db(store),
    }
    results = dict(await asyncio.gather(*(_run(name, c) for name, c in checks.items())))
    all_ok = all(bool(r["ok"]) for r in results.values())
    return all_ok, results
