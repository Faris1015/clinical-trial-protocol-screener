"""The compliance rules database — the viewer (#57) and admin authoring (#97).

Three halves, really: `app/services/rules.py`, which turns stored rows into
something a reviewer can read (threshold rendered, check kind named, severity
stated); the write path an admin authors through; and the routes serving both.

The tests that matter most here are the *agreement* ones. A rules viewer's only
value is that it says what the engine does — a page claiming "advisory" for a
rule that blocks the run would be worse than no page, because a reviewer would
trust it. So the severity a rule publishes is checked against the severity a
finding from that rule actually carries, and every rule id the Critic can emit is
checked to resolve to a listed rule.

The #97 half adds a second class of agreement test: what the *table* holds has to
be what the *engine* runs. A rule authored through the API is fed to
`run_deterministic_checks` for real, and a retired one is checked to stop firing
while staying resolvable — those two are the whole of AC 3 and AC 4.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.services.rules as rules_service
from app.exceptions import DuplicateRuleError, InvalidRuleError, RuleNotFoundError
from app.graph.nodes.critic import (
    CHECK_SEVERITY,
    SEMANTIC_RULE_ID,
    load_rules,
    run_deterministic_checks,
)
from app.persistence import SUBJECT_RULE, InMemoryAuditStore, InMemoryRuleStore, RuleRecord
from app.services.rules import (
    active_engine_rules,
    create_rule,
    list_compliance_rules,
    seed_from_file,
    set_rule_enabled,
    update_rule,
)
from tests.auth_helpers import ADMIN, REVIEWER, sign_in


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
async def store():
    """A rules table seeded from the shipped YAML, as first boot would leave it."""
    rule_store = InMemoryRuleStore()
    await rule_store.setup()
    await seed_from_file(rule_store)
    return rule_store


@pytest.fixture
def audit_store():
    return InMemoryAuditStore()


def _by_id(listing: dict) -> dict[str, dict]:
    return {rule["id"]: rule for rule in listing["rules"]}


def _criteria(**overrides):
    base: dict = {
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    base.update(overrides)
    return base


def _rule(**overrides) -> dict:
    """A valid `range` rule; override one field to make it invalid on purpose."""
    base = {
        "id": "TEST-001",
        "check": "range",
        "attribute": "age",
        "description": "Age threshold outside plausible range",
        "plain": "The age limit is not one a real patient could have.",
        "keywords": [],
        "min_plausible": 0,
        "max_plausible": 120,
    }
    base.update(overrides)
    return base


# --- seeding (AC 1) ----------------------------------------------------------


async def test_the_yaml_seeds_the_table_on_first_boot(store):
    listed = _by_id(await list_compliance_rules(store))
    for rule in load_rules():
        assert rule["id"] in listed


async def test_seeding_is_a_no_op_once_the_table_has_rows(store):
    """The whole of "the DB is the source of truth thereafter": a redeploy must
    never revert an admin's edits by re-reading the file."""
    assert await seed_from_file(store) == 0


async def test_an_admin_edit_survives_a_reseed(store, audit_store):
    """The failure the AC exists to prevent, end to end."""
    await update_rule(
        store,
        audit_store,
        "BP-001",
        _rule(check="range", attribute="systolic_bp", min_plausible=95, max_plausible=180),
        ADMIN,
    )
    await seed_from_file(store)
    assert _by_id(await list_compliance_rules(store))["BP-001"]["condition"] == (
        "95 ≤ systolic_bp ≤ 180"
    )


async def test_seeded_rules_are_attributed_to_the_seeder_not_to_a_person(store):
    """An empty `created_by` reads as missing data; a person's name would be a lie."""
    bp = _by_id(await list_compliance_rules(store))["BP-001"]
    assert bp["created_by"] == rules_service.SEED_ACTOR
    assert bp["created_at"]


async def test_a_malformed_seed_row_is_skipped_and_the_rest_still_seed(monkeypatch):
    """`RULES_PATH` can point at an operator's own file. One bad row must not stop
    the server from booting — the remaining rules still guard every screening."""
    monkeypatch.setattr(
        rules_service,
        "load_rules",
        lambda: [
            "not a mapping",
            {"no_id": True, "check": "range"},
            {"id": "BAD-001", "check": "range", "attribute": "age", "min_plausible": "x"},
            {
                "id": "OK-001",
                "check": "range",
                "attribute": "age",
                "description": "Fine",
                "min_plausible": 1,
                "max_plausible": 2,
            },
        ],
    )
    rule_store = InMemoryRuleStore()
    await rule_store.setup()
    assert await seed_from_file(rule_store) == 1
    assert [r["id"] for r in (await list_compliance_rules(rule_store))["rules"]] == [
        "OK-001",
        SEMANTIC_RULE_ID,
    ]


async def test_seeding_keeps_the_file_s_order_not_alphabetical(store):
    """The file groups rules by clinical domain and comments each group — that is
    the order the person who maintains them thinks in."""
    listed = [r["id"] for r in (await list_compliance_rules(store))["rules"]]
    assert listed[: len(load_rules())] == [rule["id"] for rule in load_rules()]


# --- the listing (#57, preserved) --------------------------------------------


async def test_a_range_rule_states_its_bounds(store):
    """The column #57 asks for: threshold/operator, not just a rationale."""
    bp = _by_id(await list_compliance_rules(store))["BP-001"]
    assert bp["condition"] == "90 ≤ systolic_bp ≤ 200"
    assert bp["check_label"] == "Plausible range"


async def test_every_rule_carries_both_rationale_layers(store):
    """A reviewer who arrived from a plain-language finding gets plain prose."""
    for rule in (await list_compliance_rules(store))["rules"]:
        assert rule["description"]
        assert rule["plain"]


async def test_a_rule_without_plain_prose_falls_back_to_its_description(store, audit_store):
    """The same fallback `critic._finding` applies, so the page and the finding
    it explains never show different wording for the same rule."""
    await create_rule(
        store, audit_store, _rule(id="X-001", plain="", description="Technical only"), ADMIN
    )
    assert _by_id(await list_compliance_rules(store))["X-001"]["plain"] == "Technical only"


async def test_the_semantic_layer_is_listed_so_its_findings_have_somewhere_to_link(store):
    """`LLM-SEM` has no row in the rules table, but the Critic stamps findings with
    it — and a finding whose rule id resolves to nothing is the exact gap #57
    closed."""
    entry = _by_id(await list_compliance_rules(store))[SEMANTIC_RULE_ID]
    assert entry["layer"] == "semantic"
    # It must not claim a fixed severity: the review assigns its own per finding.
    assert entry["severity"] == "varies"
    # And it must not offer an edit affordance for a layer nobody can author.
    assert entry["editable"] is False


async def test_the_semantic_entry_is_never_fed_to_the_deterministic_engine(store):
    """Listing it must not turn it into a rule the engine tries to run."""
    assert SEMANTIC_RULE_ID not in {rule["id"] for rule in await active_engine_rules(store)}


async def test_every_seeded_check_kind_is_one_the_engine_implements(store):
    """A rule whose check has no branch never fires."""
    for rule in await active_engine_rules(store):
        assert rule["check"] in CHECK_SEVERITY


# --- agreement with the engine -----------------------------------------------


async def test_published_severity_matches_the_severity_a_finding_would_carry(store):
    """The anti-drift test. Two rules, tripped for real, with the engine's own
    severity compared against what the page publishes — one that blocks a run and
    one that only advises, so a single hard-coded answer can't pass both."""
    listed = _by_id(await list_compliance_rules(store))
    engine = await active_engine_rules(store)

    renal = {
        f["rule_id"]: f["severity"]
        for f in run_deterministic_checks(
            _criteria(unparseable=["Adequate renal function."]), "adequate renal", engine
        )
    }
    assert renal["RENAL-001"] == listed["RENAL-001"]["severity"] == "reject"

    # No age criterion extracted, so AGE-001's required_attribute check fires.
    age = {f["rule_id"]: f["severity"] for f in run_deterministic_checks(_criteria(), "", engine)}
    assert age["AGE-001"] == listed["AGE-001"]["severity"] == "warn"


async def test_a_rule_authored_through_the_api_is_one_the_engine_actually_runs(store, audit_store):
    """AC 2 and AC 3 together: authoring extends the layer for real, not just the
    listing. A stored rule the engine could not consume would be the whole feature
    silently not working."""
    await create_rule(
        store,
        audit_store,
        _rule(id="HR-001", attribute="age", min_plausible=18, max_plausible=65),
        ADMIN,
    )
    criteria = _criteria(
        inclusion_quantitative=[
            {
                "attribute": "age",
                "operator": ">=",
                "value": 900,
                "unit": "years",
                "source_text": "age >= 900",
            }
        ]
    )
    fired = {
        f["rule_id"]
        for f in run_deterministic_checks(criteria, "", await active_engine_rules(store))
    }
    assert "HR-001" in fired


# --- validation (AC 3) -------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        ({"id": ""}, "a rule with no id is one nothing can ever cite"),
        ({"id": "HAS SPACE"}, "an id travels in a URL and a CSV cell"),
        ({"id": "-LEADING"}, "an id starts with a letter or a digit"),
        ({"id": SEMANTIC_RULE_ID}, "the semantic layer's id is reserved"),
        ({"description": ""}, "a rule with no rationale explains nothing"),
        ({"check": "regex_match"}, "an unimplemented check would never fire"),
        ({"attribute": ""}, "a range rule needs the attribute it tests"),
        ({"min_plausible": None}, "a range needs two numeric bounds"),
        ({"min_plausible": "low"}, "a bound the engine compares must be a number"),
        ({"min_plausible": True}, "True is an int in Python but not a threshold"),
        ({"min_plausible": 200, "max_plausible": 90}, "an inverted range matches nothing"),
    ],
)
def test_a_malformed_rule_is_refused_at_authoring_time(payload, because):
    """Every one of these would otherwise be a KeyError or a silent no-op in the
    middle of somebody's screening — which is exactly what AC 3 is for."""
    with pytest.raises(InvalidRuleError):
        rules_service.validate(_rule(**payload))


def test_a_keyword_driven_rule_without_keywords_is_refused():
    """It could never fire. Stored, it would look live and quietly pass everything
    — the worst kind of broken rule."""
    with pytest.raises(InvalidRuleError):
        rules_service.validate(
            {
                "id": "K-001",
                "check": "keyword_implies_criterion",
                "description": "x",
                "keywords": [],
                "required_category": "condition",
            }
        )


def test_a_string_where_a_keyword_list_belongs_is_refused():
    """`keywords: renal` is the easy slip. Iterating it would store five one-letter
    keywords, which reads as data rather than as a mistake — so it is refused
    outright rather than coerced into something plausible."""
    with pytest.raises(InvalidRuleError):
        rules_service.validate(_rule(keywords="renal"))


def test_prose_longer_than_the_cap_is_refused():
    """A rule's description is rendered in every finding it produces and in the
    listing; it is bounded rather than trusted, like every other free-text field
    this app persists."""
    with pytest.raises(InvalidRuleError):
        rules_service.validate(_rule(description="x" * 5000))


def test_keywords_are_lowercased_because_that_is_how_the_engine_compares_them():
    """The engine lowers the protocol text, never the keyword. A rule authored
    with "Renal" would silently never match."""
    fields = rules_service.validate(
        _rule(check="must_be_quantitative", attribute="egfr", keywords=["Renal", "KIDNEY"])
    )
    assert fields["keywords"] == ["renal", "kidney"]


def test_a_keyword_implies_criterion_rule_needs_no_attribute():
    """It asks whether *any* criterion covers a topic, so it has no single
    attribute to name — the one kind for which the attribute check must not fire."""
    fields = rules_service.validate(
        {
            "id": "P-001",
            "check": "keyword_implies_criterion",
            "description": "x",
            "keywords": ["pregnan"],
            "required_category": "condition",
        }
    )
    assert fields["attribute"] == ""


# --- a hand-edited database --------------------------------------------------
#
# `validate` means the API can no longer store either of these. Both stay covered
# because the rules table is a table an operator can reach with a SQL client, and
# the listing must survive what they put there — a page that raised would be the
# one that would have shown them their mistake.


async def test_an_unimplemented_check_kind_says_it_never_fires(store):
    await store.add(
        RuleRecord(id="FUTURE-001", check="regex_match", description="Someday", position=99)
    )
    entry = _by_id(await list_compliance_rules(store))["FUTURE-001"]
    assert entry["severity"] == ""
    assert "never fires" in entry["condition"]


async def test_stored_params_can_never_overwrite_a_rule_s_own_fields(store):
    """`params` is a JSON blob in a column an operator can reach with a SQL client,
    and the listing spreads it into the payload. Spread last, a row whose params
    held `{"severity": "warn"}` would publish a severity the engine does not apply
    — the one thing an audit surface must never do."""
    await store.add(
        RuleRecord(
            id="EVIL-001",
            check="range",
            description="Real description",
            attribute="age",
            params={
                "min_plausible": 1,
                "max_plausible": 2,
                "id": "SPOOFED",
                "severity": "warn",
                "enabled": False,
            },
            position=99,
        )
    )
    entry = _by_id(await list_compliance_rules(store))["EVIL-001"]
    assert entry["id"] == "EVIL-001"
    # `range` blocks a run, and that is what the page must say.
    assert entry["severity"] == "reject"
    assert entry["enabled"] is True
    # The legitimate params still come through.
    assert entry["condition"] == "1 ≤ age ≤ 2"


async def test_a_malformed_bound_renders_instead_of_raising(store):
    await store.add(
        RuleRecord(
            id="BAD-001",
            check="range",
            description="Hand-edited",
            attribute="age",
            params={"min_plausible": "x"},
            position=99,
        )
    )
    assert _by_id(await list_compliance_rules(store))["BAD-001"]["condition"] == "? ≤ age ≤ ?"


# --- authoring (AC 2) --------------------------------------------------------


async def test_a_new_rule_is_attributed_to_its_author(store, audit_store):
    created = await create_rule(store, audit_store, _rule(), ADMIN)
    assert created["created_by"] == created["updated_by"] == ADMIN.email
    assert created["created_at"]


async def test_reusing_an_existing_id_is_refused_rather_than_silently_overwriting(
    store, audit_store
):
    """A finding cites a rule id forever, so rewriting the rule behind one would
    change what every past finding means."""
    with pytest.raises(DuplicateRuleError):
        await create_rule(store, audit_store, _rule(id="BP-001"), ADMIN)


async def test_an_edit_keeps_the_original_author_and_records_the_reviser(store, audit_store):
    """Rewriting who first authored a rule would be a lie the audit log could not
    catch."""
    await create_rule(store, audit_store, _rule(), ADMIN)
    updated = await update_rule(
        store, audit_store, "TEST-001", _rule(description="Revised"), REVIEWER
    )
    assert updated["created_by"] == ADMIN.email
    assert updated["updated_by"] == REVIEWER.email
    assert updated["description"] == "Revised"


async def test_an_edit_cannot_repoint_a_rule_id(store, audit_store):
    """The URL owns the id: re-pointing one at different wording is a new rule."""
    await create_rule(store, audit_store, _rule(), ADMIN)
    updated = await update_rule(store, audit_store, "TEST-001", _rule(id="SOMETHING-ELSE"), ADMIN)
    assert updated["id"] == "TEST-001"
    assert await store.get("SOMETHING-ELSE") is None


async def test_editing_a_rule_that_does_not_exist_is_a_404(store, audit_store):
    with pytest.raises(RuleNotFoundError):
        await update_rule(store, audit_store, "NOPE-001", _rule(), ADMIN)


# --- soft retirement (AC 4) --------------------------------------------------


async def test_a_retired_rule_stops_firing(store, audit_store):
    await set_rule_enabled(store, audit_store, "BP-001", False, ADMIN)
    assert "BP-001" not in {rule["id"] for rule in await active_engine_rules(store)}


async def test_a_retired_rule_is_still_listed_so_old_findings_resolve(store, audit_store):
    """AC 4's real requirement. A run checkpointed last quarter still carries
    `BP-001`, and following that link has to land on the rule, marked retired —
    not on a "no such rule" page."""
    await set_rule_enabled(store, audit_store, "BP-001", False, ADMIN)
    listed = _by_id(await list_compliance_rules(store))
    assert listed["BP-001"]["enabled"] is False
    assert listed["BP-001"]["description"]


async def test_a_retired_rule_can_be_restored(store, audit_store):
    await set_rule_enabled(store, audit_store, "BP-001", False, ADMIN)
    await set_rule_enabled(store, audit_store, "BP-001", True, ADMIN)
    assert "BP-001" in {rule["id"] for rule in await active_engine_rules(store)}


async def test_retirement_records_who_retired_it(store, audit_store):
    result = await set_rule_enabled(store, audit_store, "BP-001", False, ADMIN)
    assert result["updated_by"] == ADMIN.email


async def test_the_active_count_reports_what_the_engine_will_run(store, audit_store):
    before = (await list_compliance_rules(store))["active"]
    await set_rule_enabled(store, audit_store, "BP-001", False, ADMIN)
    after = await list_compliance_rules(store)
    assert after["active"] == before - 1
    assert after["active"] == len(await active_engine_rules(store))


async def test_retiring_a_rule_that_does_not_exist_is_a_404(store, audit_store):
    with pytest.raises(RuleNotFoundError):
        await set_rule_enabled(store, audit_store, "NOPE-001", False, ADMIN)


# --- the audit trail (AC 5) --------------------------------------------------


async def test_every_mutation_lands_in_the_decision_index(store, audit_store):
    await create_rule(store, audit_store, _rule(), ADMIN)
    await update_rule(store, audit_store, "TEST-001", _rule(description="Revised"), ADMIN)
    await set_rule_enabled(store, audit_store, "TEST-001", False, ADMIN)
    await set_rule_enabled(store, audit_store, "TEST-001", True, ADMIN)

    page = await audit_store.list(limit=50, offset=0)
    assert [row.action for row in page.items][::-1] == [
        "rule_created",
        "rule_updated",
        "rule_disabled",
        "rule_enabled",
    ]


async def test_a_rule_entry_names_the_rule_as_its_subject_not_a_run(store, audit_store):
    """A rule mutation is about no run. Filling `thread_id` with something
    run-shaped would deep-link an auditor to a 404."""
    await create_rule(store, audit_store, _rule(), ADMIN)
    entry = (await audit_store.list(limit=1, offset=0)).items[0]
    assert entry.subject_kind == SUBJECT_RULE
    assert entry.subject_id == "TEST-001"
    assert entry.thread_id == ""


async def test_a_rule_entry_is_attributed_to_the_admin_who_made_it(store, audit_store):
    await create_rule(store, audit_store, _rule(), ADMIN)
    entry = (await audit_store.list(limit=1, offset=0)).items[0]
    assert entry.actor == ADMIN.email
    assert entry.actor_role == ADMIN.role


async def test_the_indexed_detail_states_what_the_rule_now_enforces(store, audit_store):
    """An auditor scanning the index wants the condition, not the prose — "what
    does this rule now do" is the question a threshold change raises."""
    await create_rule(store, audit_store, _rule(min_plausible=1, max_plausible=2), ADMIN)
    entry = (await audit_store.list(limit=1, offset=0)).items[0]
    assert "1 ≤ age ≤ 2" in entry.detail


# --- the routes --------------------------------------------------------------


def test_rules_require_a_session(client):
    assert client.get("/api/rules").status_code == 401


def test_a_reviewer_can_read_the_rules(client):
    sign_in(client)
    response = client.get("/api/rules")
    assert response.status_code == 200
    body = response.json()
    assert {rule["id"] for rule in body["rules"]} >= {"RENAL-001", "BP-001", SEMANTIC_RULE_ID}


def test_the_response_names_the_seed_file_but_never_its_path(client):
    """The page states which default set the table was seeded from. The absolute
    path is server topology and stays server-side."""
    sign_in(client)
    body = client.get("/api/rules").json()
    assert body["source"] == "compliance_rules.yaml"
    assert "/" not in body["source"]


def test_a_reviewer_cannot_author_a_rule(client):
    """AC 6's other half: reviewers see the same read-only page they always did."""
    sign_in(client, REVIEWER)
    assert client.post("/api/rules", json=_rule()).status_code == 403
    assert client.patch("/api/rules/BP-001", json=_rule()).status_code == 403
    assert client.patch("/api/rules/BP-001/enabled", json={"enabled": False}).status_code == 403


def test_an_admin_can_author_a_rule_end_to_end(client):
    sign_in(client, ADMIN)
    created = client.post("/api/rules", json=_rule())
    assert created.status_code == 201
    assert created.json()["id"] == "TEST-001"

    listing = client.get("/api/rules").json()
    assert "TEST-001" in {rule["id"] for rule in listing["rules"]}


def test_an_integer_bound_stays_an_integer_through_the_route(client):
    """Through the route, not through `validate` — the coercion this guards against
    happens in Pydantic, which a service-level test never reaches.

    A bare `float` on the request model turns a JSON `5` into `5.0`, and the bound
    is echoed into every rendered condition: an authored rule would read
    "5.0 ≤ egfr ≤ 150.0" directly beneath a seeded "90 ≤ systolic_bp ≤ 200". Same
    database, same page, two notations for the same kind of number."""
    sign_in(client, ADMIN)
    created = client.post(
        "/api/rules",
        json=_rule(id="EGFR-009", attribute="egfr", min_plausible=5, max_plausible=150),
    )
    assert created.status_code == 201
    assert created.json()["condition"] == "5 ≤ egfr ≤ 150"


def test_a_fractional_bound_survives_the_route_too(client):
    """The other half: ANC-001 ships with `min_plausible: 0.1`, so a real bound can
    genuinely be fractional and must not be rounded to an int."""
    sign_in(client, ADMIN)
    created = client.post(
        "/api/rules",
        json=_rule(id="ANC-009", attribute="anc", min_plausible=0.1, max_plausible=50),
    )
    assert created.status_code == 201
    assert created.json()["condition"] == "0.1 ≤ anc ≤ 50"


def test_an_admin_can_retire_a_rule_through_the_route(client):
    sign_in(client, ADMIN)
    response = client.patch("/api/rules/BP-001/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False

    listed = {rule["id"]: rule for rule in client.get("/api/rules").json()["rules"]}
    assert listed["BP-001"]["enabled"] is False


def test_a_malformed_rule_is_a_422_naming_what_is_wrong(client):
    sign_in(client, ADMIN)
    response = client.post("/api/rules", json=_rule(min_plausible=200, max_plausible=90))
    assert response.status_code == 422
    assert response.json()["error"] == "InvalidRuleError"


def test_a_duplicate_id_is_a_409(client):
    sign_in(client, ADMIN)
    response = client.post("/api/rules", json=_rule(id="BP-001"))
    assert response.status_code == 409
    assert response.json()["error"] == "DuplicateRuleError"


def test_an_unknown_field_is_refused_rather_than_dropped(client):
    """A typo'd `min_plausable` would otherwise store a rule without the bound its
    author believed they set."""
    sign_in(client, ADMIN)
    payload = _rule()
    payload["min_plausable"] = 5
    assert client.post("/api/rules", json=payload).status_code == 422


def test_editing_an_unknown_rule_through_the_route_is_a_404(client):
    sign_in(client, ADMIN)
    assert client.patch("/api/rules/NOPE-001", json=_rule()).status_code == 404


def test_there_is_no_delete_route(client):
    """A finding cites a rule id forever; deleting the row would leave every past
    finding pointing at nothing. Retirement is the only way out."""
    sign_in(client, ADMIN)
    assert client.delete("/api/rules/BP-001").status_code == 405
