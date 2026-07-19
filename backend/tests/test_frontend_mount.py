"""Single-service demo mode: the API also serves the built SPA (free-demo deploy).

`mount_frontend` is what lets one container host both the SPA and the API. These
lock its contract: it mounts only when a real bundle is present, serves the SPA
from "/", and never shadows the API routes registered before it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_frontend


def _bundle(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>SPA</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    return tmp_path


def test_no_mount_when_dist_is_none():
    app = FastAPI()
    assert mount_frontend(app, None) is False


def test_no_mount_when_index_missing(tmp_path):
    # Directory exists but has no index.html — nothing to serve.
    assert mount_frontend(FastAPI(), tmp_path) is False


def test_serves_spa_and_assets(tmp_path):
    app = FastAPI()
    assert mount_frontend(app, _bundle(tmp_path)) is True
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "<!doctype html>" in root.text.lower()

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text


def test_api_routes_win_over_the_spa_catch_all(tmp_path):
    """Routes registered before the mount must still resolve — the SPA mount at
    "/" is a catch-all and must never shadow the API."""
    app = FastAPI()

    @app.get("/api/thing")
    def thing() -> dict:
        return {"ok": True}

    mount_frontend(app, _bundle(tmp_path))
    client = TestClient(app)

    assert client.get("/api/thing").json() == {"ok": True}
    assert client.get("/").status_code == 200  # SPA still served for everything else
