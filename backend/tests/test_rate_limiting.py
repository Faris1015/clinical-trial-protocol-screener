"""Rate limiting (#15): the limiter throttles the create endpoint while liveness
stays green. The suite disables the limiter globally (conftest); this module
re-enables it locally with a tight, deterministic limit and resets its counter.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def limited_client(monkeypatch):
    # Tighten the (per-request evaluated) limit and switch the limiter on for
    # this test only, resetting the process-wide counter on both ends.
    monkeypatch.setattr(main.settings, "rate_limit_create", "2/minute")
    main.limiter.reset()
    main.limiter.enabled = True
    try:
        with TestClient(main.app, raise_server_exceptions=False) as c:
            yield c
    finally:
        main.limiter.enabled = False
        main.limiter.reset()


def _upload(client):
    return client.post(
        "/api/screenings",
        files={"file": ("p.md", b"Inclusion criteria: age >= 18", "text/markdown")},
    )


def test_create_is_throttled_after_limit(limited_client):
    assert _upload(limited_client).status_code == 200
    assert _upload(limited_client).status_code == 200
    blocked = _upload(limited_client)
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error"] == "RateLimitExceeded"
    # slowapi injects a Retry-After so clients know when to try again.
    assert "retry-after" in {k.lower() for k in blocked.headers}


def test_health_stays_green_while_create_is_throttled(limited_client):
    for _ in range(5):
        _upload(limited_client)
    # Liveness is never rate-limited — the probe must stay 200 under a flood.
    assert limited_client.get("/health").status_code == 200


def test_reads_are_not_throttled_by_the_create_limit(limited_client):
    # A generous read limit means listing repeatedly never trips the create cap.
    for _ in range(5):
        assert limited_client.get("/api/screenings").status_code == 200
