"""API hardening at the HTTP edge (#15): upload size cap, content-type rejection,
filename sanitization end-to-end, and the concurrency gate's 429 + Retry-After.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services.concurrency import ConcurrencyLimiter


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


# --- upload size cap --------------------------------------------------------


def test_oversized_body_rejected_by_content_length(client, monkeypatch):
    # Declared size over the cap → 413 before the body is read (fast path).
    monkeypatch.setattr(main.settings, "max_upload_bytes", 1024)
    big = b"x" * (5 * 1024)
    resp = client.post("/api/screenings", files={"file": ("big.pdf", big, "application/pdf")})
    assert resp.status_code == 413
    assert resp.json()["error"] == "PayloadTooLargeError"


def test_body_within_cap_is_accepted(client, monkeypatch):
    monkeypatch.setattr(main.settings, "max_upload_bytes", 1_000_000)
    resp = client.post(
        "/api/screenings",
        files={"file": ("p.md", b"Inclusion criteria: age >= 18", "text/markdown")},
    )
    assert resp.status_code == 200


# --- content-type allowlist -------------------------------------------------


def test_disallowed_content_type_rejected_415(client):
    resp = client.post("/api/screenings", files={"file": ("logo.png", b"\x89PNG\r\n", "image/png")})
    assert resp.status_code == 415
    assert resp.json()["error"] == "UnsupportedMediaTypeError"


def test_generic_type_with_allowed_extension_accepted(client):
    resp = client.post(
        "/api/screenings",
        files={"file": ("p.txt", b"Inclusion criteria: age >= 18", "application/octet-stream")},
    )
    assert resp.status_code == 200


# --- filename sanitization end-to-end --------------------------------------


def test_traversal_filename_is_sanitized_before_storage(client):
    resp = client.post(
        "/api/screenings",
        files={"file": ("../../etc/passwd", b"Inclusion criteria: age >= 18", "text/plain")},
    )
    assert resp.status_code == 200
    listed = client.get("/api/screenings").json()
    names = {r["source_filename"] for r in listed}
    assert "passwd" in names
    assert not any("/" in n or ".." in n for n in names)


# --- concurrency gate -------------------------------------------------------


def test_stream_returns_429_with_retry_after_when_saturated(client, monkeypatch):
    # Swap in a gate with no free slots; the stream route must 429 before any
    # SSE frame is sent, and advertise Retry-After.
    saturated = ConcurrencyLimiter(limit=1, retry_after_seconds=3)
    saturated.acquire()  # take the only slot
    monkeypatch.setattr(main, "active_screenings", saturated)

    upload = client.post(
        "/api/screenings",
        files={"file": ("p.md", b"Inclusion criteria: age >= 18", "text/markdown")},
    )
    thread_id = upload.json()["thread_id"]

    resp = client.get(f"/api/screenings/{thread_id}/stream")
    assert resp.status_code == 429
    assert resp.json()["error"] == "TooManyActiveScreeningsError"
    assert resp.headers["Retry-After"] == "3"


def test_approve_returns_429_when_saturated(client, monkeypatch):
    saturated = ConcurrencyLimiter(limit=1, retry_after_seconds=9)
    saturated.acquire()
    monkeypatch.setattr(main, "active_screenings", saturated)

    upload = client.post(
        "/api/screenings",
        files={"file": ("p.md", b"Inclusion criteria: age >= 18", "text/markdown")},
    )
    thread_id = upload.json()["thread_id"]

    resp = client.post(f"/api/screenings/{thread_id}/approve")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "9"


def test_health_stays_green_when_screenings_saturated(client, monkeypatch):
    saturated = ConcurrencyLimiter(limit=1)
    saturated.acquire()
    monkeypatch.setattr(main, "active_screenings", saturated)
    assert client.get("/health").status_code == 200
