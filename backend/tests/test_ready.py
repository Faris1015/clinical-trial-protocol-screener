"""Readiness probe (#6): /ready aggregates dependency checks into 200/503.

The endpoint tests stub the individual checks so the aggregation, status code,
and log-exclusion behavior are exercised deterministically (no network, no
committed data files). The per-check functions are unit-tested separately.
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import app.health as health
import app.main as main
from app.persistence import InMemoryScreeningStore


@pytest.fixture
def client():
    # `with` runs the lifespan so the persistence store (db check) is wired up.
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def _stub_checks(monkeypatch, *, llm=True, rules=True, patients=True, db=True):
    async def ok(detail):
        return {"ok": True, "detail": detail}

    async def bad(detail):
        return {"ok": False, "detail": detail}

    monkeypatch.setattr(health, "_check_llm", lambda: (ok if llm else bad)("llm"))
    monkeypatch.setattr(health, "_check_rules", lambda: (ok if rules else bad)("rules"))
    monkeypatch.setattr(health, "_check_patients", lambda: (ok if patients else bad)("patients"))
    monkeypatch.setattr(health, "_check_db", lambda _store: (ok if db else bad)("db"))


# --- endpoint aggregation ---------------------------------------------------


def test_ready_all_checks_pass_returns_200(client, monkeypatch):
    _stub_checks(monkeypatch)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"llm", "rules", "patients", "db"}
    assert all(c["ok"] for c in body["checks"].values())
    assert "version" in body and "commit" in body


def test_ready_one_failure_returns_503_naming_the_check(client, monkeypatch):
    _stub_checks(monkeypatch, llm=False)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["llm"]["ok"] is False
    # The other checks are reported and still healthy.
    assert body["checks"]["rules"]["ok"] is True
    assert body["checks"]["db"]["ok"] is True


def test_health_stays_200_when_ready_is_degraded(client, monkeypatch):
    _stub_checks(monkeypatch, llm=False, db=False)
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200  # liveness is independent


def test_ready_excluded_from_access_logs(client, monkeypatch, capsys):
    _stub_checks(monkeypatch)
    capsys.readouterr()  # drop startup/prior output
    client.get("/ready")
    out = capsys.readouterr().out
    assert "request.start" not in out
    assert "request.finish" not in out


# --- per-check behavior -----------------------------------------------------


def _patch_httpx(monkeypatch, handler):
    # Capture the real class first; the replacement builds a MockTransport client
    # from it (referencing httpx.AsyncClient inside would recurse into this stub).
    real_cls = httpx.AsyncClient
    monkeypatch.setattr(
        health.httpx,
        "AsyncClient",
        lambda **_kw: real_cls(transport=httpx.MockTransport(handler), timeout=1),
    )


async def test_check_llm_ok_when_backend_reachable(monkeypatch):
    _patch_httpx(monkeypatch, lambda _req: httpx.Response(200, json={"models": []}))
    result = await health._check_llm()
    assert result["ok"] is True


async def test_check_llm_raises_when_backend_down(monkeypatch):
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        await health._check_llm()


async def test_readiness_converts_a_hanging_check_into_a_timeout(monkeypatch):
    # A check that outlives the (shrunk) budget is reported failed, not awaited
    # forever — so one hung dependency can't wedge the probe.
    monkeypatch.setattr(health, "CHECK_TIMEOUT_S", 0.05)

    async def hang():
        await asyncio.sleep(1)
        return {"ok": True, "detail": "never"}

    async def ok(detail):
        return {"ok": True, "detail": detail}

    monkeypatch.setattr(health, "_check_llm", hang)
    monkeypatch.setattr(health, "_check_rules", lambda: ok("rules"))
    monkeypatch.setattr(health, "_check_patients", lambda: ok("patients"))
    monkeypatch.setattr(health, "_check_db", lambda _s: ok("db"))

    all_ok, checks = await health.readiness(store=InMemoryScreeningStore())
    assert all_ok is False
    assert checks["llm"]["ok"] is False
    assert "timed out" in checks["llm"]["detail"]
