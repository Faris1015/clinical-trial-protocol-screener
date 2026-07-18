"""Tests for the deterministic Critic rules — each rule should trip on its fixture."""

from app.graph.nodes.critic import load_rules, run_deterministic_checks


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


RULES = load_rules()


def test_vague_renal_criterion_rejected():
    crit = _criteria(unparseable=["Adequate renal function."])
    findings = run_deterministic_checks(crit, "adequate renal function", RULES)
    assert any(f["rule_id"] == "RENAL-001" and f["severity"] == "reject" for f in findings)


def test_quantitative_renal_criterion_passes():
    crit = _criteria(
        inclusion_quantitative=[
            {
                "attribute": "egfr",
                "operator": ">=",
                "value": 60,
                "value_high": None,
                "unit": "mL/min",
                "source_text": "eGFR >= 60",
            }
        ]
    )
    findings = run_deterministic_checks(crit, "egfr >= 60", RULES)
    assert not any(f["rule_id"] == "RENAL-001" for f in findings)


def test_vague_keyword_only_in_unparseable_not_in_text_ignored():
    """Regression: a hallucinated `unparseable` entry must not trip the rule.

    A small model dumped "organ function" into unparseable for a protocol that
    never mentions it — HEPATIC-001 fired and looped the parser to escalation.
    The rule must key off the protocol text, not the model's unparseable list.
    """
    crit = _criteria(unparseable=["organ function must be adequate"])
    findings = run_deterministic_checks(crit, "type 2 diabetes; hba1c between 7 and 10", RULES)
    assert not any(f["rule_id"] == "HEPATIC-001" for f in findings)


def test_implausible_bp_rejected():
    crit = _criteria(
        exclusion_quantitative=[
            {
                "attribute": "systolic_bp",
                "operator": ">",
                "value": 400,
                "value_high": None,
                "unit": "mmHg",
                "source_text": "SBP > 400",
            }
        ]
    )
    findings = run_deterministic_checks(crit, "", RULES)
    assert any(f["rule_id"] == "BP-001" and f["severity"] == "reject" for f in findings)


def test_unit_scaled_platelet_threshold_rejected():
    """A model that expands "100 x 10^9/L" to 1e11 must be caught: such a
    threshold excludes every record (all counts are far below it)."""
    crit = _criteria(
        exclusion_quantitative=[
            {
                "attribute": "platelets",
                "operator": "<",
                "value": 100_000_000_000.0,
                "value_high": None,
                "unit": "x10^9/L",
                "source_text": "Platelet count < 100 x 10^9/L",
            }
        ]
    )
    findings = run_deterministic_checks(crit, "platelet count < 100 x 10^9/L", RULES)
    assert any(f["rule_id"] == "PLT-001" and f["severity"] == "reject" for f in findings)


def test_plausible_platelet_threshold_passes():
    crit = _criteria(
        exclusion_quantitative=[
            {
                "attribute": "platelets",
                "operator": "<",
                "value": 100,
                "value_high": None,
                "unit": "x10^9/L",
                "source_text": "Platelet count < 100 x 10^9/L",
            }
        ]
    )
    findings = run_deterministic_checks(crit, "platelet count < 100 x 10^9/L", RULES)
    assert not any(f["rule_id"] == "PLT-001" for f in findings)


def test_unit_scaled_anc_threshold_rejected():
    crit = _criteria(
        exclusion_quantitative=[
            {
                "attribute": "anc",
                "operator": "<",
                "value": 1_500_000_000.0,
                "value_high": None,
                "unit": "x10^9/L",
                "source_text": "ANC < 1.5 x 10^9/L",
            }
        ]
    )
    findings = run_deterministic_checks(crit, "anc < 1.5 x 10^9/L", RULES)
    assert any(f["rule_id"] == "ANC-001" and f["severity"] == "reject" for f in findings)


def test_missing_age_bound_warns():
    findings = run_deterministic_checks(_criteria(), "", RULES)
    assert any(f["rule_id"] == "AGE-001" and f["severity"] == "warn" for f in findings)


def test_pregnancy_keyword_without_criterion_rejected():
    crit = _criteria()
    findings = run_deterministic_checks(crit, "women of childbearing potential", RULES)
    assert any(f["rule_id"] == "PREG-001" for f in findings)


def test_pregnancy_criterion_with_wrong_category_still_covers():
    """PREG-001 verifies the pregnancy exclusion was captured, not that the model
    tagged it category='condition' — a weak model may mislabel it as biomarker."""
    crit = _criteria(
        exclusion_categorical=[
            {
                "category": "biomarker",  # mislabeled, but clearly about pregnancy
                "value": "pregnant or breastfeeding",
                "negated": False,
                "source_text": "Women of childbearing potential who are pregnant or breastfeeding.",
            }
        ]
    )
    findings = run_deterministic_checks(crit, "women of childbearing potential", RULES)
    assert not any(f["rule_id"] == "PREG-001" for f in findings)
