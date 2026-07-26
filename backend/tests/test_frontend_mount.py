"""Single-service demo mode: the API also serves the built frontend (free-demo deploy).

`mount_frontend` is what lets one container host both the frontend and the API.
These lock its contract: it mounts only when a real bundle is present, serves the
app from "/", resolves the per-route directories a Next static export emits, and
never shadows the API routes registered before it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_frontend


def _bundle(tmp_path):
    """A miniature of the frontend's `next build` output (`frontend/out/`)."""
    (tmp_path / "index.html").write_text("<!doctype html><title>TrialGate</title>")
    (tmp_path / "404.html").write_text("<!doctype html><title>404</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    # `trailingSlash: true` exports every non-root route as its own directory.
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "index.html").write_text("<!doctype html><title>Runs</title>")
    return tmp_path


def test_no_mount_when_dist_is_none():
    app = FastAPI()
    assert mount_frontend(app, None) is False


def test_no_mount_when_index_missing(tmp_path):
    # Directory exists but has no index.html — nothing to serve.
    assert mount_frontend(FastAPI(), tmp_path) is False


def test_serves_the_app_shell_and_assets(tmp_path):
    app = FastAPI()
    assert mount_frontend(app, _bundle(tmp_path)) is True
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "<!doctype html>" in root.text.lower()

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text


def test_serves_exported_route_directories(tmp_path):
    """Non-root routes must resolve in demo mode too.

    A Next static export writes `<route>/index.html` rather than a single SPA
    shell, so the demo image only keeps working for the routes #51/#53 add if the
    mount resolves a directory to its index — with or without a trailing slash.
    """
    app = FastAPI()
    mount_frontend(app, _bundle(tmp_path))
    client = TestClient(app)

    for path in ("/runs", "/runs/"):
        res = client.get(path, follow_redirects=True)
        assert res.status_code == 200, path
        assert "Runs" in res.text, path


def test_unknown_path_gets_the_exported_404(tmp_path):
    """No SPA-shell fallback: an unexported path is a real 404, and the export's
    own 404.html is what the user sees."""
    app = FastAPI()
    mount_frontend(app, _bundle(tmp_path))

    res = TestClient(app).get("/definitely-not-a-route")
    assert res.status_code == 404
    assert "404" in res.text


def test_api_routes_win_over_the_frontend_catch_all(tmp_path):
    """Routes registered before the mount must still resolve — the frontend mount
    at "/" is a catch-all and must never shadow the API."""
    app = FastAPI()

    @app.get("/api/thing")
    def thing() -> dict:
        return {"ok": True}

    mount_frontend(app, _bundle(tmp_path))
    client = TestClient(app)

    assert client.get("/api/thing").json() == {"ok": True}
    assert client.get("/").status_code == 200  # frontend still served at the root
