"""Critic layer 2 — the LLM semantic review (#16).

The deterministic layer is covered in test_critic_rules; here we cover the
LLM pass: that its findings are stamped LLM-SEM and merge into the same
compliance_findings list, that a reject loops the Parser, and that a degraded
LLM never aborts a screening (unavailable -> non-blocking warn, malformed ->
dropped). The model itself is faked, so these run offline and deterministically.
"""

import app.graph.nodes.critic as critic_mod
from app.exceptions import LLMUnavailableError
from app.graph.state import ScreenerState
from app.schemas.review import Finding, SemanticReview
from tests.fakes import FakeChatModel

# age >= 18 inclusion alongside age > 65 exclusion — the acceptance-criteria case.
INCONSISTENT_CRITERIA = {
    "trial_title": "Age-Inconsistent Trial",
    "inclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18,
            "value_high": None,
            "unit": "years",
            "source_text": "Age 18 years or older.",
        }
    ],
    "inclusion_categorical": [],
    "exclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">",
            "value": 65,
            "value_high": None,
            "unit": "years",
            "source_text": "Exclude patients older than 65.",
        }
    ],
    "exclusion_categorical": [],
    "unparseable": [],
}


def _state(criteria: dict | None = INCONSISTENT_CRITERIA, text: str = "protocol") -> ScreenerState:
    return {
        "raw_protocol_text": text,
        "source_filename": "p.md",
        "parsed_criteria": criteria,
        "compliance_passed": False,
        "critic_feedback": None,
        "parse_attempts": 1,
        "compliance_findings": [],
        "matched_patients": [],
        "events": [],
        "current_step": "critiquing",
    }


def _patch_review(monkeypatch, scripted: list) -> None:
    monkeypatch.setattr(critic_mod, "get_llm", lambda: FakeChatModel(scripted))


def test_semantic_review_flags_age_inconsistency(monkeypatch):
    review = SemanticReview(
        findings=[
            Finding(
                severity="reject",
                message="Inclusion age >= 18 conflicts with exclusion age > 65.",
            )
        ]
    )
    _patch_review(monkeypatch, [review])

    findings = critic_mod.run_llm_semantic_review(_state())

    assert findings == [
        {
            "rule_id": "LLM-SEM",
            "severity": "reject",
            "message": "Inclusion age >= 18 conflicts with exclusion age > 65.",
        }
    ]


def test_rule_id_is_forced_even_if_model_omits_it(monkeypatch):
    """The schema has no rule_id field; the Critic stamps LLM-SEM itself."""
    _patch_review(monkeypatch, [SemanticReview(findings=[Finding(severity="warn", message="x")])])
    findings = critic_mod.run_llm_semantic_review(_state())
    assert all(f["rule_id"] == "LLM-SEM" for f in findings)


def test_critic_node_rejects_and_loops_on_semantic_finding(monkeypatch):
    """A semantic reject with a clean deterministic layer still blocks and routes
    back to the Parser, and the LLM finding rides in critic_feedback."""
    monkeypatch.setattr(critic_mod, "run_deterministic_checks", lambda *a, **k: [])
    _patch_review(
        monkeypatch,
        [SemanticReview(findings=[Finding(severity="reject", message="age bounds inconsistent")])],
    )

    update = critic_mod.critic_node(_state())

    assert update["compliance_passed"] is False
    assert any(f["rule_id"] == "LLM-SEM" for f in update["compliance_findings"])
    assert "LLM-SEM" in update["critic_feedback"]
    assert "age bounds inconsistent" in update["critic_feedback"]
    assert update["current_step"] == "parsing"


def test_semantic_findings_merge_with_deterministic(monkeypatch):
    """Both layers' findings land in the same list."""
    monkeypatch.setattr(
        critic_mod,
        "run_deterministic_checks",
        lambda *a, **k: [{"rule_id": "AGE-001", "severity": "warn", "message": "det"}],
    )
    _patch_review(
        monkeypatch,
        [SemanticReview(findings=[Finding(severity="warn", message="sem")])],
    )

    update = critic_mod.critic_node(_state())
    rule_ids = {f["rule_id"] for f in update["compliance_findings"]}
    assert rule_ids == {"AGE-001", "LLM-SEM"}
    # Only warnings -> nothing blocking -> the screening still passes.
    assert update["compliance_passed"] is True


def test_warn_severity_does_not_block(monkeypatch):
    monkeypatch.setattr(critic_mod, "run_deterministic_checks", lambda *a, **k: [])
    _patch_review(
        monkeypatch,
        [SemanticReview(findings=[Finding(severity="warn", message="minor concern")])],
    )
    update = critic_mod.critic_node(_state())
    assert update["compliance_passed"] is True
    assert update["current_step"] == "awaiting_approval"


def test_unavailable_llm_yields_nonblocking_warn(monkeypatch):
    """A down backend degrades to a visible warn, never an abort or silent skip."""
    _patch_review(monkeypatch, [LLMUnavailableError("backend down")])

    findings = critic_mod.run_llm_semantic_review(_state())

    assert len(findings) == 1
    assert findings[0]["rule_id"] == "LLM-SEM"
    assert findings[0]["severity"] == "warn"
    assert "unavailable" in findings[0]["message"].lower()


def test_malformed_output_is_dropped(monkeypatch):
    """Output that can't be coerced to SemanticReview is dropped, not raised."""
    _patch_review(monkeypatch, ["not a review object"])
    assert critic_mod.run_llm_semantic_review(_state()) == []


def test_no_criteria_short_circuits_without_llm(monkeypatch):
    def _boom() -> object:
        raise AssertionError("get_llm must not be called when there are no criteria")

    monkeypatch.setattr(critic_mod, "get_llm", _boom)
    assert critic_mod.run_llm_semantic_review(_state(criteria=None)) == []
