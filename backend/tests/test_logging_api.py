"""Request correlation at the API edge: X-Request-ID handling and thread_id logging.

Format-agnostic on purpose — assertions check for values/event names that appear
in both the console and JSON renderers, so they hold regardless of LOG_FORMAT.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def test_response_mints_a_request_id_header(client):
    response = client.get("/health")
    assert response.headers.get("x-request-id")


def test_response_echoes_client_supplied_request_id(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert response.headers["x-request-id"] == "trace-abc"


def test_screening_creation_logs_carry_the_thread_id(client, capsys):
    response = client.post(
        "/api/screenings", files={"file": ("p.md", b"Inclusion criteria: age >= 18")}
    )
    thread_id = response.json()["thread_id"]

    created = [line for line in capsys.readouterr().out.splitlines() if "screening.created" in line]
    assert created, "expected a screening.created log line"
    assert thread_id in created[0]


def test_upload_never_logs_protocol_text(client, capsys):
    secret = "SUPER-SECRET-PROTOCOL-BODY-DO-NOT-LOG"
    body = f"Inclusion criteria: age >= 18. {secret}".encode()
    client.post("/api/screenings", files={"file": ("p.md", body)})

    assert secret not in capsys.readouterr().out
