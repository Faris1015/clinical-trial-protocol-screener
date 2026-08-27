"""Durable cross-run term mapping cache service (#105).

Provides context-management for the active `TermStore`, model ID resolution,
and administrative cache invalidation with audit logging.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.config import get_settings
from app.logging_config import get_logger
from app.services import audit
from app.services.llm import configured_model

if TYPE_CHECKING:
    from app.auth import Principal
    from app.persistence import AuditStore, TermStore

log = get_logger("terms")

_ACTIVE_TERM_STORE: ContextVar[TermStore | None] = ContextVar("active_term_store", default=None)


@contextmanager
def using_term_store(store: TermStore | None) -> Iterator[None]:
    """Scope an active TermStore for the current execution stack / graph run."""
    token = _ACTIVE_TERM_STORE.set(store)
    try:
        yield
    finally:
        _ACTIVE_TERM_STORE.reset(token)


def current_term_store() -> TermStore | None:
    """The active TermStore for this call stack, or None."""
    return _ACTIVE_TERM_STORE.get()


def active_model_id() -> str:
    """The active LLM model identifier for term-mapping cache keying.

    Ensures that switching models (e.g. Ollama meditron -> Anthropic Claude -> stub)
    never silently reuses another model's clinical judgements.
    """
    settings = get_settings()
    provider = settings.llm_provider
    return configured_model(provider)


async def purge_cache(
    term_store: TermStore,
    audit_store: AuditStore,
    principal: Principal,
    *,
    model_id: str | None = None,
) -> dict:
    """Invalidate cached term mappings and record the action in the audit log (#105).

    Guarded at the admin rung at the edge.
    """
    purged_count = await term_store.purge(model_id=model_id)
    scope = f"for model '{model_id}'" if model_id else "across all models"
    detail = f"Purged {purged_count} cached term mapping(s) {scope}"
    occurred_at = datetime.now(UTC).isoformat()
    await audit.record(
        audit_store,
        "cache_purged",
        thread_id="",
        actor=principal,
        detail=detail,
        occurred_at=occurred_at,
        source_filename="",
        subject_kind="terms",
        subject_id=model_id or "all",
    )
    log.info("terms.cache_purged", purged=purged_count, model_id=model_id, actor=principal.email)
    return {"purged": purged_count, "model_id": model_id}
