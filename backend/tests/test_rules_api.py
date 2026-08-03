"""The compliance rules viewer (#57) — the listing and the route behind it.

Two halves: `app/services/rules.py`, which turns the YAML rows the Critic runs
into something a reviewer can read (threshold rendered, check kind named,
severity stated); and `GET /api/rules`, which serves them.

The tests that matter most here are the *agreement* ones. A rules viewer's only
value is that it says what the engine does — a page claiming "advisory" for a
rule that blocks the run would be worse than no page, because a reviewer would
trust it. So the severity a rule publishes is checked against the severity a
finding from that rule actually carries, and every rule id the Critic can emit is
checked to resolve to a listed rule.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.exceptions import DataStoreError
from app.graph.nodes.critic import (
    CHECK_SEVERITY,
    SEMANTIC_RULE_ID,
    load_rules,
    run_deterministic_checks,
)
from app.services import rules as rules_service
from app.services.rules import list_compliance_rules
from tests.auth_helpers import sign_in


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


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


def _run_findings(criteria: dict, text: str) -> dict[str, str]:
    """Rule id → the severity the engine actually stamped on its finding."""
    return {
        f["rule_id"]: f["severity"] for f in run_deterministic_checks(criteria, text, load_rules())
    }


# --- the listing -------------------------------------------------------------


def test_every_rule_in_the_file_is_listed():
    listed = _by_id(list_compliance_rules())
    for rule in load_rules():
        assert rule["id"] in listed


def test_a_range_rule_states_its_bounds():
    """The column the issue asks for: threshold/operator, not just a rationale."""
    bp = _by_id(list_compliance_rules())["BP-001"]
    assert bp["condition"] == "90 ≤ systolic_bp ≤ 200"
    assert bp["check_label"] == "Plausible range"


def test_every_rule_carries_both_rationale_layers():
    """A reviewer who arrived from a plain-language finding gets plain prose."""
    for rule in list_compliance_rules()["rules"]:
        assert rule["description"]
        assert rule["plain"]


def test_a_rule_without_plain_prose_falls_back_to_its_description(monkeypatch):
    """The same fallback `critic._finding` applies, so the page and the finding
    it explains never show different wording for the same rule."""
    monkeypatch.setattr(
        rules_service,
        "load_rules",
        lambda: [{"id": "X-001", "check": "required_attribute", "description": "Technical only"}],
    )
    assert _by_id(list_compliance_rules())["X-001"]["plain"] == "Technical only"


# --- agreement with the engine ----------------------------------------------


def test_published_severity_matches_the_severity_a_finding_would_carry():
    """The anti-drift test. Two rules, tripped for real, with the engine's own
    severity compared against what the page publishes — one that blocks a run and
    one that only advises, so a single hard-coded answer can't pass both."""
    listed = _by_id(list_compliance_rules())

    renal = _run_findings(_criteria(unparseable=["Adequate renal function."]), "adequate renal")
    assert renal["RENAL-001"] == listed["RENAL-001"]["severity"] == "reject"

    # No age criterion extracted, so AGE-001's required_attribute check fires.
    age = _run_findings(_criteria(), "")
    assert age["AGE-001"] == listed["AGE-001"]["severity"] == "warn"


def test_the_semantic_layer_is_listed_so_its_findings_have_somewhere_to_link():
    """`LLM-SEM` has no row in the rules file, but the Critic stamps findings with
    it — and a finding whose rule id resolves to nothing is the exact gap this
    feature closes."""
    entry = _by_id(list_compliance_rules())[SEMANTIC_RULE_ID]
    assert entry["layer"] == "semantic"
    # It must not claim a fixed severity: the review assigns its own per finding.
    assert entry["severity"] == "varies"


def test_the_semantic_entry_is_not_fed_to_the_deterministic_engine():
    """Listing it must not turn it into a rule the engine tries to run — the
    engine reads the file, the viewer reads the file plus this."""
    assert SEMANTIC_RULE_ID not in {rule["id"] for rule in load_rules()}


def test_every_check_kind_in_the_file_is_one_the_engine_implements():
    """A rule whose check has no branch never fires. It is listed as such rather
    than hidden, but the shipped file should not contain one."""
    for rule in load_rules():
        assert rule["check"] in CHECK_SEVERITY


# --- a hand-edited rules file ------------------------------------------------


def test_a_row_that_is_not_a_rule_is_dropped_rather_than_raising(monkeypatch):
    """`RULES_PATH` can point at an operator's own file. A malformed row must not
    take down the page that would have shown them the rest of it."""
    monkeypatch.setattr(
        rules_service,
        "load_rules",
        lambda: [
            "not a mapping",
            {"no_id": True, "check": "range"},
            {"id": "OK-001", "check": "range", "min_plausible": 1, "max_plausible": 2},
        ],
    )
    listing = list_compliance_rules()
    assert [rule["id"] for rule in listing["rules"]] == ["OK-001", SEMANTIC_RULE_ID]


def test_an_unimplemented_check_kind_says_it_never_fires(monkeypatch):
    monkeypatch.setattr(
        rules_service,
        "load_rules",
        lambda: [{"id": "FUTURE-001", "check": "regex_match", "description": "Someday"}],
    )
    entry = _by_id(list_compliance_rules())["FUTURE-001"]
    assert entry["severity"] == ""
    assert "never fires" in entry["condition"]


def test_a_string_where_a_keyword_list_belongs_is_not_split_into_letters(monkeypatch):
    """`keywords: renal` is the easy YAML slip. Iterating it would publish five
    one-letter keywords, which reads as data rather than as a mistake."""
    monkeypatch.setattr(
        rules_service,
        "load_rules",
        lambda: [{"id": "STR-001", "check": "range", "keywords": "renal"}],
    )
    assert _by_id(list_compliance_rules())["STR-001"]["keywords"] == []


def test_a_malformed_bound_renders_instead_of_raising(monkeypatch):
    monkeypatch.setattr(
        rules_service,
        "load_rules",
        lambda: [{"id": "BAD-001", "check": "range", "attribute": "age", "min_plausible": "x"}],
    )
    assert _by_id(list_compliance_rules())["BAD-001"]["condition"] == "? ≤ age ≤ ?"


# --- the route ---------------------------------------------------------------


def test_rules_require_a_session(client):
    assert client.get("/api/rules").status_code == 401


def test_a_reviewer_can_read_the_rules(client):
    sign_in(client)
    response = client.get("/api/rules")
    assert response.status_code == 200
    body = response.json()
    assert {rule["id"] for rule in body["rules"]} >= {"RENAL-001", "BP-001", SEMANTIC_RULE_ID}


def test_the_response_names_the_rules_file_but_never_its_path(client):
    """The page states which file produced the thresholds — an instance can run
    amended ones. The absolute path is server topology and stays server-side."""
    sign_in(client)
    body = client.get("/api/rules").json()
    assert body["source"] == "compliance_rules.yaml"
    assert "/" not in body["source"]


def test_a_missing_rules_file_is_a_503_not_a_500(client, monkeypatch):
    """Settings validates the file at startup, but it can vanish underneath a
    running server — the same failure the Critic would hit on the next run."""

    def gone() -> list[dict]:
        raise DataStoreError(f"Compliance rules unavailable at {Path('/gone/rules.yaml')}")

    monkeypatch.setattr(rules_service, "load_rules", gone)
    sign_in(client)
    response = client.get("/api/rules")
    assert response.status_code == 503
    assert response.json()["error"] == "DataStoreError"
