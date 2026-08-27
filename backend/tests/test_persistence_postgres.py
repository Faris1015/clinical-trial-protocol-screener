"""The Postgres stores, against a real Postgres (#97).

`CHECKPOINT_BACKEND=postgres` is the multi-replica production target, and until
now nothing executed a line of it: the `postgres` extra is optional, so the suite
ran without psycopg and every `Postgres*Store` was dead weight to the test run.
Sqlite's twin passing says nothing about these — the two engines disagree on
exactly the statements this feature added.

Skipped unless `POSTGRES_TEST_DSN` is set, so a developer's `pytest` stays fast
and needs no database. CI sets it against a service container; `make test-pg`
runs it locally.

What is worth the round trip, and why each is here rather than assumed:

**`ON CONFLICT (id) DO NOTHING`** — sqlite spells the same idea `INSERT OR
IGNORE`, so the seed path is genuinely different code on the two engines. It
exists for the replicas-boot-together race, which only happens on this backend.

**`int(enabled)` against an INTEGER column** — psycopg adapts a Python `bool` to
postgres `boolean`, and postgres refuses to assign that to `integer`. Sqlite
takes either. A bare bool here is a `DatatypeMismatch` at the moment an admin
retires a rule, on production only.

**`ADD COLUMN IF NOT EXISTS` + the subject backfill** — the sqlite store
introspects `PRAGMA table_info` instead, so again: different code, and the case
that matters is upgrading a deployment whose `audit_events` predates #97.
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.persistence import AuditDecision, AuditFilter, RuleRecord, TermRecord, open_persistence

# Defaulted to "" rather than left as None so it stays a `str` for the psycopg
# calls below; the skip is what guarantees nothing here runs against the empty one.
DSN = os.environ.get("POSTGRES_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="POSTGRES_TEST_DSN is unset — see the module docstring"
)


def _settings() -> Settings:
    return Settings(_env_file=None, checkpoint_backend="postgres", postgres_dsn=DSN)


async def _clean() -> None:
    """Drop this feature's tables so each test starts from nothing.

    The checkpointer's own tables are left alone — `AsyncPostgresSaver.setup()`
    is idempotent and re-creating them per test would dominate the runtime.
    """
    from psycopg import AsyncConnection

    conn = await AsyncConnection.connect(DSN, autocommit=True)
    try:
        for table in ("compliance_rules", "audit_events", "screenings", "app_meta", "term_mappings"):
            await conn.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    await _clean()
    persistence = await open_persistence(_settings())
    try:
        yield persistence
    finally:
        await persistence.aclose()


def _rule(rule_id: str, **overrides) -> RuleRecord:
    base = {
        "id": rule_id,
        "check": "range",
        "description": f"{rule_id} description",
        "attribute": "systolic_bp",
        "keywords": ["bp"],
        "params": {"min_plausible": 90, "max_plausible": 200},
        "created_by": "trialgate-seed",
        "created_at": "2026-08-12T00:00:00+00:00",
    }
    base.update(overrides)
    return RuleRecord(**base)  # type: ignore[arg-type]


# --- the rules table ---------------------------------------------------------


async def test_rules_round_trip_through_the_json_columns(pg):
    """`keywords` and `params` are JSON *text* on both engines rather than a
    native JSON type — sqlite has none, and one encoding in the application beats
    two in the schema. This is the half that proves postgres agrees."""
    await pg.rules.add(_rule("BP-001"))
    stored = await pg.rules.get("BP-001")
    assert stored is not None
    assert stored.keywords == ["bp"]
    assert stored.params == {"min_plausible": 90, "max_plausible": 200}
    # Read back as a bool, not as the 1 the INTEGER column holds.
    assert stored.enabled is True


async def test_seed_fills_an_empty_table_then_never_again(pg):
    assert await pg.rules.seed([_rule("BP-001"), _rule("BP-002")]) == 2
    assert await pg.rules.seed([_rule("BP-003")]) == 0
    assert [r.id for r in await pg.rules.list()] == ["BP-001", "BP-002"]


async def test_seeding_a_duplicate_id_is_ignored_rather_than_raising(pg):
    """The conflict-ignoring insert, driven through `seed` itself.

    Its reason for existing is the cross-replica race — two replicas boot, both
    find the table empty, both insert, and without the clause the loser raises
    IntegrityError out of `open_persistence` so that replica never starts. That
    race cannot be staged deterministically from one process, but it reaches the
    *same statement* a duplicate id inside one seed set does, so this is what
    holds the clause in place: drop it and this fails.

    It is also a real case on its own — a hand-edited `RULES_PATH` file with a
    repeated id must not take down startup. First one wins, matching
    `InMemoryRuleStore.seed`'s `setdefault` and sqlite's `INSERT OR IGNORE`.
    """
    written = await pg.rules.seed(
        [
            _rule("BP-001", description="the winner"),
            _rule("BP-002"),
            _rule("BP-001", description="the loser"),
        ]
    )
    # The return is what was attempted, not what landed — see `RuleStore.seed`.
    assert written == 3

    stored = await pg.rules.list()
    assert [r.id for r in stored] == ["BP-001", "BP-002"]
    survivor = await pg.rules.get("BP-001")
    assert survivor is not None
    assert survivor.description == "the winner"


async def test_two_replicas_seeding_at_once_both_survive(pg):
    """The race itself, run concurrently on two connections.

    Non-deterministic by nature — whether the two interleave inside the window is
    up to the scheduler — so it is the test above that pins the behaviour. This
    one is here because the failure it looks for is a *startup crash*, and the
    cheapest proof that two replicas can boot against one fresh database is to
    boot two against one fresh database.
    """
    import asyncio

    second = await open_persistence(_settings())
    try:
        results = await asyncio.gather(
            pg.rules.seed([_rule("BP-001"), _rule("BP-002")]),
            second.rules.seed([_rule("BP-001"), _rule("BP-002")]),
            return_exceptions=True,
        )
        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a replica failed to start: {raised}"
        assert [r.id for r in await pg.rules.list()] == ["BP-001", "BP-002"]
    finally:
        await second.aclose()


async def test_retiring_a_rule_does_not_trip_the_bool_integer_mismatch(pg):
    """psycopg adapts a Python bool to postgres `boolean`, which postgres will not
    assign to an `integer` column — so `set_enabled` casts. Without the cast this
    is a DatatypeMismatch the moment an admin retires a rule in production, and
    sqlite would never have shown it."""
    await pg.rules.add(_rule("BP-001"))
    await pg.rules.set_enabled("BP-001", False, "admin@test.local", "2026-08-12T02:00:00+00:00")

    retired = await pg.rules.get("BP-001")
    assert retired is not None
    assert retired.enabled is False
    assert retired.updated_by == "admin@test.local"
    # Out of the engine's view, still in the viewer's — AC 4, on the real backend.
    assert await pg.rules.list(include_disabled=False) == []
    assert [r.id for r in await pg.rules.list()] == ["BP-001"]

    await pg.rules.set_enabled("BP-001", True, "admin@test.local", "2026-08-12T03:00:00+00:00")
    restored = await pg.rules.get("BP-001")
    assert restored is not None and restored.enabled is True


async def test_replace_keeps_creation_and_position(pg):
    await pg.rules.add(_rule("BP-001", position=3, created_by="first@test.local"))
    await pg.rules.replace(
        _rule(
            "BP-001",
            description="revised",
            position=99,
            created_by="impostor@test.local",
            created_at="2099-01-01T00:00:00+00:00",
            updated_by="second@test.local",
        )
    )
    rule = await pg.rules.get("BP-001")
    assert rule is not None
    assert rule.description == "revised"
    assert rule.updated_by == "second@test.local"
    assert rule.created_by == "first@test.local"
    assert rule.position == 3


async def test_next_position_appends_after_everything(pg):
    assert await pg.rules.next_position() == 0
    await pg.rules.add(_rule("BP-001", position=7))
    assert await pg.rules.next_position() == 8


# --- the audit index's subject columns ---------------------------------------


async def test_a_rule_decision_names_the_rule_and_no_run(pg):
    await pg.audit.record(
        AuditDecision(
            thread_id="",
            action="rule_created",
            actor="admin@test.local",
            actor_role="admin",
            occurred_at="2026-08-12T05:00:00+00:00",
            detail="range — 5 ≤ egfr ≤ 150",
            subject_kind="rule",
            subject_id="EGFR-009",
        )
    )
    (row,) = (await pg.audit.list(limit=10, offset=0)).items
    assert (row.subject_kind, row.subject_id, row.thread_id) == ("rule", "EGFR-009", "")

    filtered = await pg.audit.list(limit=10, offset=0, filters=AuditFilter(action="rule_created"))
    assert filtered.total == 1


async def test_a_run_decision_defaults_its_subject_to_the_run(pg):
    await pg.audit.record(
        AuditDecision(
            thread_id="run-1",
            action="approved",
            actor="a@test.local",
            actor_role="reviewer",
            occurred_at="2026-08-12T04:00:00+00:00",
            detail="Cleared the gate",
        )
    )
    (row,) = (await pg.audit.list(limit=10, offset=0)).items
    assert (row.subject_kind, row.subject_id) == ("screening", "run-1")


async def test_setup_migrates_and_backfills_a_pre_existing_audit_table():
    """The upgrade path that actually matters: a postgres deployment whose
    `audit_events` predates the subject columns. Leaving `subject_id` empty would
    break the deep link on every decision the org has ever recorded."""
    from psycopg import AsyncConnection

    await _clean()
    conn = await AsyncConnection.connect(DSN, autocommit=True)
    try:
        await conn.execute(
            "CREATE TABLE audit_events ("
            "id BIGSERIAL PRIMARY KEY, thread_id TEXT NOT NULL, action TEXT NOT NULL, "
            "actor TEXT NOT NULL, actor_role TEXT NOT NULL, occurred_at TEXT NOT NULL, "
            "detail TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, "
            "source_filename TEXT NOT NULL DEFAULT '')"
        )
        await conn.execute(
            "INSERT INTO audit_events "
            "(thread_id, action, actor, actor_role, occurred_at, detail) "
            "VALUES ('run-legacy', 'approved', 'a@test.local', 'reviewer', "
            "'2026-01-01T00:00:00+00:00', 'Cleared the gate')"
        )
    finally:
        await conn.close()

    persistence = await open_persistence(_settings())
    try:
        (row,) = (await persistence.audit.list(limit=10, offset=0)).items
        assert row.thread_id == "run-legacy"
        assert row.subject_kind == "screening"
        assert row.subject_id == "run-legacy"

        # setup() runs on every boot, so the backfill has to be re-runnable.
        await persistence.audit.setup()
        (again,) = (await persistence.audit.list(limit=10, offset=0)).items
        assert again.subject_id == "run-legacy"
    finally:
        await persistence.aclose()


# --- Stale Reminders and Meta Table Persistence (#103) -----------------------


async def test_postgres_screening_store_parked_runs_and_meta(pg):
    # Create runs with various statuses
    await pg.store.create("t1", "p1.pdf", "text1")
    await pg.store.set_status("t1", "routing")

    await pg.store.create("t2", "p2.pdf", "text2")
    await pg.store.set_status("t2", "awaiting_approval")
    await pg.store.mark_gate_entered("t2", "2026-01-01T10:00:00+00:00")

    await pg.store.create("t3", "p3.pdf", "text3")
    await pg.store.set_status("t3", "done")

    await pg.store.create("t4", "p4.pdf", "text4")
    await pg.store.set_status("t4", "escalated")
    await pg.store.mark_gate_entered("t4", "2026-01-01T11:00:00+00:00")

    parked = await pg.store.list_parked()
    assert len(parked) == 2
    thread_ids = [r.thread_id for r in parked]
    assert "t2" in thread_ids
    assert "t4" in thread_ids
    assert "t1" not in thread_ids
    assert "t3" not in thread_ids

    # Test mark_reminder_sent
    await pg.store.mark_reminder_sent("t2", "2026-01-01T12:00:00+00:00")
    r2 = await pg.store.get_record("t2")
    assert r2 is not None
    assert r2.gate_entered_at == "2026-01-01T10:00:00+00:00"
    assert r2.last_reminder_at == "2026-01-01T12:00:00+00:00"

    # Test re-entering gate clears last_reminder_at
    await pg.store.mark_gate_entered("t2", "2026-01-01T13:00:00+00:00")
    r2_updated = await pg.store.get_record("t2")
    assert r2_updated is not None
    assert r2_updated.gate_entered_at == "2026-01-01T13:00:00+00:00"
    assert r2_updated.last_reminder_at is None

    # Test meta store
    assert (await pg.store.get_meta("last_digest_at")) is None
    await pg.store.set_meta("last_digest_at", "2026-01-01T08:00:00+00:00")
    assert (await pg.store.get_meta("last_digest_at")) == "2026-01-01T08:00:00+00:00"
    # Test update (upsert)
    await pg.store.set_meta("last_digest_at", "2026-01-02T08:00:00+00:00")
    assert (await pg.store.get_meta("last_digest_at")) == "2026-01-02T08:00:00+00:00"


# --- the terms table (#105) --------------------------------------------------


async def test_postgres_term_store_lifecycle(pg):
    """PostgresTermStore round-trips term mappings, supports upsert and purge."""
    assert pg.terms is not None
    assert await pg.terms.count() == 0

    records = [
        TermRecord("nsclc", "lung cancer", "pg-model-1", "match", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "hypertension", "pg-model-1", "no_match", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "mass in lung", "pg-model-1", "uncertain", "2026-01-01T00:00:00+00:00"),
        TermRecord("nsclc", "lung cancer", "pg-model-2", "match", "2026-01-01T00:00:00+00:00"),
    ]
    await pg.terms.set_many(records)

    assert await pg.terms.count() == 4
    assert await pg.terms.count(model_id="pg-model-1") == 3
    assert await pg.terms.count(model_id="pg-model-2") == 1

    # Single lookup
    rec = await pg.terms.get("nsclc", "mass in lung", "pg-model-1")
    assert rec is not None
    assert rec.verdict == "uncertain"

    # Batch lookup
    pairs = [("nsclc", "lung cancer"), ("nsclc", "hypertension"), ("nsclc", "mass in lung")]
    results = await pg.terms.get_many(pairs, "pg-model-1")
    assert results == {
        ("nsclc", "lung cancer"): "match",
        ("nsclc", "hypertension"): "no_match",
        ("nsclc", "mass in lung"): "uncertain",
    }

    # Upsert: overwrite with updated verdict
    updated = [TermRecord("nsclc", "mass in lung", "pg-model-1", "match", "2026-01-02T00:00:00+00:00")]
    await pg.terms.set_many(updated)
    rec_updated = await pg.terms.get("nsclc", "mass in lung", "pg-model-1")
    assert rec_updated is not None
    assert rec_updated.verdict == "match"

    # Purge by model_id
    purged_1 = await pg.terms.purge(model_id="pg-model-1")
    assert purged_1 == 3
    assert await pg.terms.count(model_id="pg-model-1") == 0
    assert await pg.terms.count(model_id="pg-model-2") == 1

    # Purge all
    purged_all = await pg.terms.purge()
    assert purged_all == 1
    assert await pg.terms.count() == 0
