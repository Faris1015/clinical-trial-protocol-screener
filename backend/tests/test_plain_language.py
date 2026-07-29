"""Human-readable Critic and Matcher output (#52).

Every result the pipeline shows a reviewer is dual-layer: the technical wording
(rule ids, attributes, operators) plus a plain-language `explanation` per item
and a `summary` per result. These lock the contract the frontend's plain /
technical toggle reads, and — the part worth testing hardest — that the plain
layer never says something the statuses underneath it don't: it is rendered from
the same deterministic comparison, not from a second opinion.
"""

from typing import cast

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
from app.graph.nodes.critic import load_rules, run_deterministic_checks, summarize_compliance
from app.graph.nodes.matcher import evaluate_patient, matcher_node, summarize_cohort
from app.graph.state import ScreenerState, initial_state
from app.schemas.review import Finding, SemanticReview
from tests.fakes import FakeChatModel

RULES = load_rules()


def _criteria(**overrides) -> dict:
    base: dict = {
        "trial_title": "T",
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    base.update(overrides)
    return base


def _quant(attribute: str, operator: str, value: float, unit: str, **overrides) -> dict:
    base = {
        "attribute": attribute,
        "operator": operator,
        "value": value,
        "value_high": None,
        "unit": unit,
        "source_text": f"{attribute} {operator} {value} {unit}",
    }
    base.update(overrides)
    return base


def _cat(value: str, negated: bool = False, category: str = "diagnosis") -> dict:
    return {"category": category, "value": value, "negated": negated, "source_text": value}


def _patient(pid: str = "PT-1", name: str = "Alice", **fields) -> dict:
    base = {"id": pid, "name": name, "labs": {}, "diagnoses": [], "medications": [], "history": []}
    base.update(fields)
    return base


def _result_for(evaluation: dict, index: int = 0) -> dict:
    result: dict = evaluation["criterion_results"][index]
    return result


# --- Critic: per-finding explanations ---------------------------------------


def test_deterministic_finding_carries_both_layers():
    """The technical message and the plain explanation are separate strings —
    the reviewer-facing one comes from the rule's `plain` prose."""
    findings = run_deterministic_checks(
        _criteria(unparseable=["Adequate renal function."]), "adequate renal function", RULES
    )
    renal = next(f for f in findings if f["rule_id"] == "RENAL-001")

    assert "FDA Guidance" in renal["message"]  # technical layer keeps the citation
    assert renal["explanation"] != renal["message"]
    assert "kidney function" in renal["explanation"]
    # No rule ids, operators or attribute names leak into the plain layer.
    assert "RENAL-001" not in renal["explanation"]


def test_range_finding_explanation_quotes_the_number_it_read():
    """A units slip is only actionable if the reviewer sees the offending value."""
    findings = run_deterministic_checks(
        _criteria(
            exclusion_quantitative=[
                _quant(
                    "platelets",
                    "<",
                    100_000_000_000.0,
                    "x10^9/L",
                    source_text="Platelet count < 100 x 10^9/L",
                )
            ]
        ),
        "platelet count < 100 x 10^9/L",
        RULES,
    )
    plt = next(f for f in findings if f["rule_id"] == "PLT-001")

    assert "100000000000" in plt["explanation"]
    assert "Platelet count < 100 x 10^9/L" in plt["explanation"]


def test_explanation_falls_back_to_description_when_a_rule_has_no_plain():
    """A rule added without plain prose still produces a usable finding."""
    rule = {
        "id": "CUSTOM-001",
        "attribute": "age",
        "description": "Protocol must state an explicit lower age bound",
        "check": "required_attribute",
    }
    findings = run_deterministic_checks(_criteria(), "", [rule])
    assert findings[0]["explanation"] == rule["description"]


def test_every_shipped_rule_has_plain_prose():
    """The fallback exists for safety, not as the normal path — the shipped rule
    set is what reviewers actually read."""
    assert [r["id"] for r in RULES if not r.get("plain")] == []


# --- Critic: the result summary ---------------------------------------------


def test_blocked_summary_names_the_problem_and_its_rule():
    findings = [
        {
            "rule_id": "HEPATIC-001",
            "severity": "reject",
            "message": "technical",
            "explanation": "The protocol describes organ function in words.",
        }
    ]
    summary = summarize_compliance(findings)

    assert summary.startswith("❌ Blocked")
    assert "The protocol describes organ function in words." in summary
    # Provenance survives into the plain layer — the finding stays auditable.
    assert "(HEPATIC-001)" in summary


def test_blocked_summary_counts_multiple_problems():
    findings = [
        {"rule_id": "A-1", "severity": "reject", "message": "m", "explanation": "first"},
        {"rule_id": "B-2", "severity": "reject", "message": "m", "explanation": "second"},
    ]
    summary = summarize_compliance(findings)
    assert "2 problems" in summary
    assert "first" in summary and "second" not in summary  # the rest are in the list


def test_cleared_summary_mentions_advisory_notes():
    findings = [{"rule_id": "AGE-001", "severity": "warn", "message": "m", "explanation": "e"}]
    summary = summarize_compliance(findings)
    assert summary.startswith("✅ Cleared")
    assert "1 advisory note" in summary


def test_cleared_summary_with_no_findings_says_so():
    summary = summarize_compliance([])
    assert summary.startswith("✅ Cleared")
    assert "nothing to fix" in summary


def _critic_state(criteria: dict, text: str = "protocol") -> ScreenerState:
    return cast(
        ScreenerState,
        {
            **initial_state(text, "p.md"),
            "parsed_criteria": criteria,
            "parse_attempts": 1,
            "current_step": "critiquing",
        },
    )


def test_critic_node_emits_a_summary_matching_its_verdict(monkeypatch):
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _s: [])
    blocked = critic_mod.critic_node(
        _critic_state(
            _criteria(unparseable=["Adequate renal function."]), "adequate renal function"
        )
    )
    assert blocked["compliance_passed"] is False
    assert blocked["compliance_summary"].startswith("❌ Blocked")

    monkeypatch.setattr(critic_mod, "run_deterministic_checks", lambda *a, **k: [])
    passed = critic_mod.critic_node(_critic_state(_criteria()))
    assert passed["compliance_passed"] is True
    assert passed["compliance_summary"].startswith("✅ Cleared")


def test_critic_feedback_stays_technical(monkeypatch):
    """The plain layer is for humans; the Parser retry loop still gets rule ids
    and the engine's own wording, which is what it can act on."""
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _s: [])
    update = critic_mod.critic_node(
        _critic_state(
            _criteria(unparseable=["Adequate renal function."]), "adequate renal function"
        )
    )
    finding = next(f for f in update["compliance_findings"] if f["rule_id"] == "RENAL-001")

    assert "[RENAL-001]" in update["critic_feedback"]
    assert finding["message"] in update["critic_feedback"]
    assert finding["explanation"] not in update["critic_feedback"]


# --- Critic: the LLM semantic layer ----------------------------------------


def test_semantic_finding_keeps_its_own_explanation(monkeypatch):
    review = SemanticReview(
        findings=[
            Finding(
                severity="reject",
                message="inclusion age >= 18 conflicts with exclusion age > 65",
                explanation="The protocol says patients must be 18 or older but also excludes "
                "anyone over 65, which is a confusing way to state an age window.",
            )
        ]
    )
    monkeypatch.setattr(critic_mod, "get_llm", lambda: FakeChatModel([review]))

    finding = critic_mod.run_llm_semantic_review(_critic_state(_criteria()))[0]
    assert finding["explanation"].startswith("The protocol says patients must be 18 or older")
    assert finding["message"] == "inclusion age >= 18 conflicts with exclusion age > 65"


def test_semantic_finding_without_an_explanation_falls_back_to_the_message(monkeypatch):
    """A weak model that skips the plain layer must not produce a blank line in
    the reviewer's view — nor drop the finding on validation."""
    review = SemanticReview(findings=[Finding(severity="warn", message="units look wrong")])
    monkeypatch.setattr(critic_mod, "get_llm", lambda: FakeChatModel([review]))

    finding = critic_mod.run_llm_semantic_review(_critic_state(_criteria()))[0]
    assert finding["explanation"] == "units look wrong"


def test_degraded_semantic_review_explains_itself(monkeypatch):
    from app.exceptions import LLMUnavailableError

    monkeypatch.setattr(
        critic_mod, "get_llm", lambda: FakeChatModel([LLMUnavailableError("backend down")])
    )
    finding = critic_mod.run_llm_semantic_review(_critic_state(_criteria()))[0]
    assert finding["severity"] == "warn"
    assert finding["explanation"]
    assert "LLM" not in finding["explanation"]  # plain layer stays jargon-free


# --- Matcher: per-criterion explanations -----------------------------------


def test_inclusion_pass_explanation_states_value_and_requirement():
    criteria = _criteria(inclusion_quantitative=[_quant("egfr", ">=", 60, "mL/min/1.73m2")])
    evaluation = evaluate_patient(_patient(labs={"egfr": 72.0}), criteria)
    result = _result_for(evaluation)

    assert result["status"] == "pass"
    assert result["explanation"] == (
        "The patient's eGFR is 72 mL/min/1.73m2, and the trial asks for at least 60 mL/min/1.73m2."
    )


def test_inclusion_fail_explanation_contrasts_value_with_requirement():
    criteria = _criteria(inclusion_quantitative=[_quant("egfr", ">=", 60, "mL/min/1.73m2")])
    result = _result_for(evaluate_patient(_patient(labs={"egfr": 42.0}), criteria))

    assert result["status"] == "fail"
    assert "The patient's eGFR is 42 mL/min/1.73m2" in result["explanation"]
    assert "but the trial asks for at least 60" in result["explanation"]


def test_between_operator_reads_as_a_range():
    criteria = _criteria(
        inclusion_quantitative=[_quant("age", "between", 18, "years", value_high=65)]
    )
    result = _result_for(evaluate_patient(_patient(labs={"age": 40}), criteria))
    assert "between 18 and 65 years" in result["explanation"]


def test_missing_lab_explanation_says_it_could_not_be_checked():
    criteria = _criteria(inclusion_quantitative=[_quant("hba1c", "<=", 10, "%")])
    result = _result_for(evaluate_patient(_patient(labs={}), criteria))

    assert result["status"] == "unknown"
    assert result["explanation"] == "No HbA1c value is on file, so this could not be checked."


def test_exclusion_explanation_distinguishes_ruled_out_from_not_ruled_out():
    criteria = _criteria(exclusion_quantitative=[_quant("platelets", "<", 100, "x10^9/L")])

    ruled_out = _result_for(evaluate_patient(_patient(labs={"platelets": 80.0}), criteria))
    assert ruled_out["status"] == "fail"
    assert "which the trial excludes (below 100 x10^9/L)" in ruled_out["explanation"]

    kept = _result_for(evaluate_patient(_patient(labs={"platelets": 250.0}), criteria))
    assert kept["status"] == "pass"
    assert "does not rule them out" in kept["explanation"]


def test_categorical_explanation_quotes_the_matching_record_term():
    """The evidence is the point: a reviewer needs to see what in the record
    satisfied the criterion, not the criterion read back to them."""
    criteria = _criteria(inclusion_categorical=[_cat("non-small cell lung cancer")])
    evaluation = evaluate_patient(
        _patient(diagnoses=["non-small cell lung cancer stage IV"]), criteria
    )
    explanation = _result_for(evaluation)["explanation"]

    assert "non-small cell lung cancer stage IV" in explanation
    assert "which counts as “non-small cell lung cancer”" in explanation
    assert explanation.endswith("which the trial requires.")


def test_semantically_mapped_term_is_named_in_the_explanation():
    criteria = _criteria(
        exclusion_categorical=[_cat("prior platinum chemotherapy", category="prior_treatment")]
    )
    verdicts = {("prior platinum chemotherapy", "carboplatin, 2023-04"): "match"}
    evaluation = evaluate_patient(
        _patient(medications=["carboplatin, 2023-04"]), criteria, verdicts
    )
    result = _result_for(evaluation)

    assert result["status"] == "fail"
    assert "carboplatin, 2023-04" in result["explanation"]
    assert "which the trial excludes" in result["explanation"]


def test_absent_exclusion_explanation_says_it_does_not_apply():
    criteria = _criteria(exclusion_categorical=[_cat("prior chemotherapy")])
    result = _result_for(evaluate_patient(_patient(), criteria))
    assert result["status"] == "pass"
    assert result["explanation"] == (
        "Nothing in the records points to “prior chemotherapy”, so this exclusion does not apply."
    )


def test_negated_inclusion_explanations_read_the_right_way_round():
    criteria = _criteria(inclusion_categorical=[_cat("active infection", negated=True)])

    clean = _result_for(evaluate_patient(_patient(), criteria))
    assert clean["status"] == "pass"
    assert "which is what the trial requires" in clean["explanation"]

    infected = _result_for(evaluate_patient(_patient(history=["active infection"]), criteria))
    assert infected["status"] == "fail"
    assert "requires patients not to have it" in infected["explanation"]


def test_uncertain_explanation_names_the_term_a_human_must_judge():
    criteria = _criteria(inclusion_categorical=[_cat("autoimmune disease", category="condition")])
    verdicts = {("autoimmune disease", "chronic inflammatory condition"): "uncertain"}
    result = _result_for(
        evaluate_patient(_patient(history=["chronic inflammatory condition"]), criteria, verdicts)
    )

    assert result["status"] == "unknown"
    assert "chronic inflammatory condition" in result["explanation"]
    assert "someone has to judge it" in result["explanation"]


# --- Matcher: per-patient summaries ----------------------------------------


def test_matching_patient_summary_covers_inclusions_and_the_exclusion():
    criteria = _criteria(
        inclusion_quantitative=[_quant("age", ">=", 18, "years")],
        inclusion_categorical=[_cat("non-small cell lung cancer")],
        exclusion_categorical=[_cat("prior chemotherapy", category="prior_treatment")],
    )
    evaluation = evaluate_patient(
        _patient(labs={"age": 61}, diagnoses=["non-small cell lung cancer"]), criteria
    )

    assert evaluation["eligible"] is True
    assert evaluation["summary"] == (
        "Alice matches — meets all 2 inclusion criteria; "
        "the one exclusion (prior chemotherapy) does not apply."
    )


def test_matching_patient_summary_with_several_exclusions():
    criteria = _criteria(
        inclusion_quantitative=[_quant("age", ">=", 18, "years")],
        exclusion_categorical=[_cat("prior chemotherapy"), _cat("active infection")],
    )
    evaluation = evaluate_patient(_patient(labs={"age": 61}), criteria)

    assert evaluation["summary"] == (
        "Alice matches — meets the one inclusion criterion; none of the 2 exclusions apply."
    )


def test_ineligible_patient_summary_says_why_not():
    criteria = _criteria(
        inclusion_quantitative=[
            _quant("age", ">=", 18, "years"),
            _quant("egfr", ">=", 60, "mL/min"),
        ]
    )
    evaluation = evaluate_patient(_patient(labs={"age": 61, "egfr": 30.0}), criteria)

    assert evaluation["eligible"] is False
    assert evaluation["summary"] == (
        "Alice does not match — does not meet 1 of 2 inclusion criteria (eGFR)."
    )


def test_excluded_patient_summary_names_the_exclusion():
    criteria = _criteria(exclusion_categorical=[_cat("prior chemotherapy")])
    evaluation = evaluate_patient(_patient(history=["prior chemotherapy, 2021"]), criteria)

    assert evaluation["summary"] == (
        "Alice does not match — is ruled out by 1 exclusion (prior chemotherapy)."
    )


def test_needs_review_summary_outranks_the_verdict():
    """A patient with an unresolved criterion has no verdict yet, so the summary
    must say "needs a human check" even though every decided criterion passed."""
    criteria = _criteria(
        inclusion_quantitative=[
            _quant("age", ">=", 18, "years"),
            _quant("egfr", ">=", 60, "mL/min"),
        ]
    )
    evaluation = evaluate_patient(_patient(labs={"age": 61}), criteria)

    assert evaluation["needs_review"] is True
    assert evaluation["summary"] == (
        "Alice needs a human check — 1 of 2 criteria could not be judged from the records (eGFR)."
    )


def test_summary_caps_the_criteria_it_names():
    criteria = _criteria(
        inclusion_quantitative=[
            _quant("egfr", ">=", 60, "mL/min"),
            _quant("hba1c", "<=", 8, "%"),
            _quant("bmi", "<=", 30, ""),
        ]
    )
    evaluation = evaluate_patient(
        _patient(labs={"egfr": 10.0, "hba1c": 12.0, "bmi": 40.0}), criteria
    )
    assert "eGFR, HbA1c, and 1 more" in evaluation["summary"]


def test_summary_falls_back_to_the_patient_id_when_unnamed():
    criteria = _criteria(inclusion_quantitative=[_quant("age", ">=", 18, "years")])
    evaluation = evaluate_patient({**_patient(labs={"age": 61}), "name": None}, criteria)
    assert evaluation["summary"].startswith("PT-1 matches")


def test_single_criterion_protocol_reads_grammatically():
    """ "meets all 1 inclusion criteria" / "1 of 1 criteria" read as bugs to the
    reviewer, so a one-criterion protocol gets its own wording."""
    criteria = _criteria(inclusion_quantitative=[_quant("age", ">=", 18, "years")])

    matched = evaluate_patient(_patient(labs={"age": 61}), criteria)
    assert matched["summary"] == "Alice matches — meets the one inclusion criterion."

    failed = evaluate_patient(_patient(labs={"age": 9}), criteria)
    assert failed["summary"] == (
        "Alice does not match — does not meet the one inclusion criterion (age)."
    )

    undecided = evaluate_patient(_patient(labs={}), criteria)
    assert undecided["summary"] == (
        "Alice needs a human check — the one criterion could not be judged from the records (age)."
    )


def test_summary_for_a_protocol_with_no_criteria():
    evaluation = evaluate_patient(_patient(), _criteria())
    assert evaluation["eligible"] is False
    assert "no criteria to check against" in evaluation["summary"]


# --- Matcher: the cohort summary -------------------------------------------


def test_cohort_summary_uses_the_same_three_buckets_as_the_table():
    """needs_review wins over eligible, exactly as the cohort table's buckets do."""
    evaluations = [
        {"eligible": True, "needs_review": False},
        {"eligible": True, "needs_review": True},  # counts as review, not a match
        {"eligible": False, "needs_review": False},
    ]
    assert summarize_cohort(evaluations) == (
        "Screened 3 patients: 1 matching this protocol, 1 needing a human check, "
        "and 1 not matching."
    )


def test_cohort_summary_with_no_patients():
    assert summarize_cohort([]) == "No patient records were available to screen."


def test_matcher_node_emits_the_cohort_summary(monkeypatch):
    criteria = _criteria(inclusion_quantitative=[_quant("age", ">=", 18, "years")])
    patients = [
        _patient("PT-1", "Alice", labs={"age": 61}),
        _patient("PT-2", "Bo", labs={"age": 9}),
    ]
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: patients)

    state = cast(
        ScreenerState,
        {
            **initial_state("x", "p.md"),
            "parsed_criteria": criteria,
            "compliance_passed": True,
            "current_step": "matching",
        },
    )
    update = matcher_node(state)

    assert update["match_summary"] == (
        "Screened 2 patients: 1 matching this protocol, 0 needing a human check, "
        "and 1 not matching."
    )
    # Every evaluation carries its own plain layer alongside the raw statuses.
    assert all(e["summary"] for e in update["matched_patients"])
    assert all(r["explanation"] for e in update["matched_patients"] for r in e["criterion_results"])
