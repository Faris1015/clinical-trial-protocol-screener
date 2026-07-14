"""Node instrumentation: every node run logs start, duration, and outcome."""

import json

import pytest

from app.config import get_settings
from app.graph.builder import _instrument
from app.logging_config import clear_contextvars, configure_logging


@pytest.fixture(autouse=True)
def json_logging(monkeypatch):
    clear_contextvars()
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    get_settings.cache_clear()
    configure_logging()
    yield
    clear_contextvars()
    get_settings.cache_clear()


def _events(capsys):
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_instrument_logs_start_and_finish_with_duration_and_outcome(capsys):
    wrapped = _instrument("router", lambda state: {"current_step": "parsing"})

    result = wrapped({"raw_protocol_text": "x"})

    assert result == {"current_step": "parsing"}
    events = _events(capsys)
    start = next(e for e in events if e["event"] == "node.start")
    finish = next(e for e in events if e["event"] == "node.finish")
    assert start["node"] == "router"
    assert finish["node"] == "router"
    assert finish["outcome"] == "parsing"
    assert isinstance(finish["duration_ms"], int | float)


def test_instrument_logs_error_and_reraises(capsys):
    def boom(state):
        raise RuntimeError("node blew up")

    wrapped = _instrument("matcher", boom)

    with pytest.raises(RuntimeError, match="node blew up"):
        wrapped({})

    events = _events(capsys)
    error = next(e for e in events if e["event"] == "node.error")
    assert error["node"] == "matcher"
    assert error["level"] == "error"
    assert "duration_ms" in error
