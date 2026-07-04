"""Tests for the deterministic Critic rules — each rule should trip on its fixture."""
from app.graph.nodes.critic import load_rules, run_deterministic_checks


def _criteria(**overrides):
    base = {
        "inclusion_quantitative": [], "inclusion_categorical": [],
        "exclusion_quantitative": [], "exclusion_categorical": [], "unparseable": [],
    }
    base.update(overrides)
    return base


RULES = load_rules()


def test_vague_renal_criterion_rejected():
    crit = _criteria(unparseable=["Adequate renal function."])
    findings = run_deterministic_checks(crit, "adequate renal function", RULES)
    assert any(f["rule_id"] == "RENAL-001" and f["severity"] == "reject" for f in findings)


def test_quantitative_renal_criterion_passes():
    crit = _criteria(inclusion_quantitative=[
        {"attribute": "egfr", "operator": ">=", "value": 60, "value_high": None,
         "unit": "mL/min", "source_text": "eGFR >= 60"}])
    findings = run_deterministic_checks(crit, "egfr >= 60", RULES)
    assert not any(f["rule_id"] == "RENAL-001" for f in findings)


def test_implausible_bp_rejected():
    crit = _criteria(exclusion_quantitative=[
        {"attribute": "systolic_bp", "operator": ">", "value": 400, "value_high": None,
         "unit": "mmHg", "source_text": "SBP > 400"}])
    findings = run_deterministic_checks(crit, "", RULES)
    assert any(f["rule_id"] == "BP-001" and f["severity"] == "reject" for f in findings)


def test_missing_age_bound_warns():
    findings = run_deterministic_checks(_criteria(), "", RULES)
    assert any(f["rule_id"] == "AGE-001" and f["severity"] == "warn" for f in findings)


def test_pregnancy_keyword_without_criterion_rejected():
    crit = _criteria()
    findings = run_deterministic_checks(crit, "women of childbearing potential", RULES)
    assert any(f["rule_id"] == "PREG-001" for f in findings)
