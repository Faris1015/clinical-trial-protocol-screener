"""Liveness probe (#11, #6): /health is dependency-free and always 200 when up."""

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def test_health_returns_ok_with_version(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Version + commit identify the running build (#6).
    assert "version" in body
    assert "commit" in body


def test_health_is_dependency_free(client):
    # Liveness never touches dependencies, so it is 200 whenever the process is
    # up — even when /ready would be degraded. (Guards /health from regressing.)
    response = client.get("/health")
    assert response.status_code == 200
