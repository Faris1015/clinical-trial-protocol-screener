"""Admin API tests for the durable cross-run term mapping cache (#105).

Verifies role-based access control (admin-only), cache purging by model and in
full, and audit trail record creation upon invalidation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.persistence import TermRecord
from tests.auth_helpers import ADMIN, REVIEWER, sign_in


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


async def test_purge_cache_unauthenticated_returns_401(client):
    res = client.post("/api/terms/cache/purge", json={})
    assert res.status_code == 401

    res_del = client.delete("/api/terms/cache")
    assert res_del.status_code == 401


async def test_purge_cache_reviewer_returns_403(client):
    sign_in(client, REVIEWER)
    res = client.post("/api/terms/cache/purge", json={})
    assert res.status_code == 403

    res_del = client.delete("/api/terms/cache")
    assert res_del.status_code == 403


async def test_purge_cache_admin_purges_and_audits(client):
    # Populate the term store
    terms = main._terms()
    audit_store = main._audit()
    await terms.set_many(
        [
            TermRecord("nsclc", "lung cancer", "model-1", "match", "2026-01-01T00:00:00+00:00"),
            TermRecord("nsclc", "adenocarcinoma", "model-1", "match", "2026-01-01T00:00:00+00:00"),
            TermRecord("nsclc", "lung cancer", "model-2", "match", "2026-01-01T00:00:00+00:00"),
        ]
    )
    assert await terms.count() == 3

    sign_in(client, ADMIN)

    # Purge for model-1 only
    res = client.post("/api/terms/cache/purge", json={"model_id": "model-1"})
    assert res.status_code == 200
    data = res.json()
    assert data["purged"] == 2
    assert data["model_id"] == "model-1"

    # Remaining in store
    assert await terms.count() == 1
    assert await terms.count(model_id="model-2") == 1
    assert await terms.count(model_id="model-1") == 0

    # Purge remaining via DELETE endpoint
    res_del = client.delete("/api/terms/cache")
    assert res_del.status_code == 200
    data_del = res_del.json()
    assert data_del["purged"] == 1
    assert data_del["model_id"] is None
    assert await terms.count() == 0

    # Check audit records
    audit_page = await audit_store.list(limit=10, offset=0)
    purge_events = [r for r in audit_page.items if r.action == "cache_purged"]
    assert len(purge_events) >= 2
    assert any("model 'model-1'" in e.detail for e in purge_events)
    assert any("across all models" in e.detail for e in purge_events)
