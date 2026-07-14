"""Structured logging, configured once from Settings.

One `configure_logging()` call wires structlog for the whole process:
console renderer in dev, one JSON object per line in prod (LOG_FORMAT, see #1).
`request_id` and `thread_id` are bound into contextvars by the API layer and
merged into every log line, so a screening's full server-side story is one
`thread_id` filter away.

PHI hygiene: never log protocol text or patient records at INFO or above —
log sizes, counts, and IDs instead, even though the data here is synthetic.
"""

import logging
from typing import Any

import structlog

from app.config import get_settings

# structlog's contextvars helpers are re-exported so callers bind correlation
# IDs without importing structlog internals directly.
bind_contextvars = structlog.contextvars.bind_contextvars
clear_contextvars = structlog.contextvars.clear_contextvars

BoundLogger = structlog.stdlib.BoundLogger


def configure_logging() -> None:
    """Install the process-wide structlog configuration from Settings.

    Idempotent: safe to call more than once (e.g. app import + tests). The
    renderer and level are re-read from Settings on each call so tests can
    toggle LOG_FORMAT/LOG_LEVEL and see the effect.
    """
    settings = get_settings()

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        # Off so a re-configure in tests (or after import) actually takes effect;
        # the caching win is negligible at this app's log volume.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a logger; `name` becomes the `logger` field when set.

    Binding a name realizes the logger against the *current* configuration, so
    this module self-configures on import (below) to guarantee the contextvars
    merger is installed before any module-level `log = get_logger(...)` binds —
    otherwise those loggers would silently drop `request_id`/`thread_id`.
    """
    logger = structlog.get_logger()
    return logger.bind(logger=name) if name is not None else logger


# Install a baseline configuration at import so any module binding a logger at
# import time gets contextvar merging. The API layer calls configure_logging()
# again after settings resolve — idempotent, and picks up LOG_FORMAT/LOG_LEVEL.
configure_logging()
