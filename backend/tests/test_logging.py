"""Structured logging: JSON rendering, contextvar correlation, and level filtering.

These tests reconfigure structlog per-case (LOG_FORMAT/LOG_LEVEL) and capture
stdout, which is where the PrintLoggerFactory writes.
"""

import json

import pytest

from app.config import get_settings
from app.logging_config import (
    bind_contextvars,
    clear_contextvars,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def reset_logging(monkeypatch):
    """Isolate each test's logging config and contextvars from the others."""
    clear_contextvars()
    for var in ("LOG_FORMAT", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    clear_contextvars()
    get_settings.cache_clear()


def _configure(monkeypatch, *, fmt="json", level="INFO"):
    monkeypatch.setenv("LOG_FORMAT", fmt)
    monkeypatch.setenv("LOG_LEVEL", level)
    get_settings.cache_clear()
    configure_logging()


def _last_json(capsys):
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_json_format_emits_one_parseable_object_per_line(monkeypatch, capsys):
    _configure(monkeypatch, fmt="json")
    get_logger("api").info("screening.created", text_chars=1234)

    obj = _last_json(capsys)
    assert obj["event"] == "screening.created"
    assert obj["text_chars"] == 1234
    assert obj["level"] == "info"
    assert obj["logger"] == "api"
    assert "timestamp" in obj


def test_bound_contextvars_ride_into_every_line(monkeypatch, capsys):
    _configure(monkeypatch, fmt="json")
    bind_contextvars(request_id="req-1", thread_id="thread-9")
    get_logger("parser").info("parser.extracted")

    obj = _last_json(capsys)
    assert obj["request_id"] == "req-1"
    assert obj["thread_id"] == "thread-9"


def test_clear_contextvars_drops_correlation_ids(monkeypatch, capsys):
    _configure(monkeypatch, fmt="json")
    bind_contextvars(thread_id="thread-9")
    clear_contextvars()
    get_logger("api").info("request.start")

    obj = _last_json(capsys)
    assert "thread_id" not in obj


def test_level_filtering_suppresses_below_threshold(monkeypatch, capsys):
    _configure(monkeypatch, fmt="json", level="WARNING")
    log = get_logger("critic")
    log.info("critic.passed")
    log.warning("critic.rejected", blocking=2)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line)["event"] for line in lines]
    assert "critic.passed" not in events
    assert "critic.rejected" in events


def test_console_format_is_not_json(monkeypatch, capsys):
    _configure(monkeypatch, fmt="console")
    get_logger("api").info("screening.created")

    out = capsys.readouterr().out
    assert "screening.created" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])
