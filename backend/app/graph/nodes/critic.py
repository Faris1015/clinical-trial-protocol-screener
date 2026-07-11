"""Agent 3: Regulatory Critic — hybrid deterministic + LLM compliance guardrail.

Layer 1 runs auditable rule checks from the rules file configured in Settings.
Layer 2 (LLM semantic review of contradictions rules can't express) plugs in
via run_llm_semantic_review. A rejection routes back to the Parser with
structured feedback; after MAX_PARSE_ATTEMPTS (Settings) the graph escalates
to a human instead of looping forever.
"""

import yaml

from app.config import get_settings
from app.exceptions import DataStoreError
from app.graph.state import ScreenerState, event


def load_rules() -> list[dict]:
    """Load the compliance rules; a missing or malformed file is a DataStoreError.

    Settings validates existence at startup, but the file can still disappear
    or be corrupted while the server runs.
    """
    path = get_settings().rules_path
    try:
        rules: list[dict] = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise DataStoreError(f"Compliance rules unavailable at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise DataStoreError(f"Compliance rules at {path} are not valid YAML: {exc}") from exc
    if not isinstance(rules, list):
        raise DataStoreError(f"Compliance rules at {path} must be a YAML list of rules")
    return rules


def _all_quantitative(criteria: dict) -> list[dict]:
    quantitative: list[dict] = (
        criteria["inclusion_quantitative"] + criteria["exclusion_quantitative"]
    )
    return quantitative


def run_deterministic_checks(criteria: dict, raw_text: str, rules: list[dict]) -> list[dict]:
    findings: list[dict] = []
    quantitative = _all_quantitative(criteria)
    categorical = criteria["inclusion_categorical"] + criteria["exclusion_categorical"]

    for rule in rules:
        check = rule["check"]

        if check == "must_be_quantitative":
            in_unparseable = any(
                any(k in u.lower() for k in rule["keywords"]) for u in criteria["unparseable"]
            )
            has_quant = any(c["attribute"] == rule["attribute"] for c in quantitative)
            if in_unparseable and not has_quant:
                findings.append(
                    {
                        "rule_id": rule["id"],
                        "severity": "reject",
                        "message": f"{rule['description']} — found only vague language, "
                        "no numeric threshold.",
                    }
                )

        elif check == "range":
            for c in quantitative:
                if c["attribute"] != rule["attribute"]:
                    continue
                if not (rule["min_plausible"] <= c["value"] <= rule["max_plausible"]):
                    findings.append(
                        {
                            "rule_id": rule["id"],
                            "severity": "reject",
                            "message": f"{rule['description']}: extracted {c['value']} {c['unit']} "
                            f"from '{c['source_text']}'",
                        }
                    )

        elif check == "required_attribute":
            if not any(c["attribute"] == rule["attribute"] for c in quantitative):
                findings.append(
                    {
                        "rule_id": rule["id"],
                        "severity": "warn",
                        "message": rule["description"],
                    }
                )

        elif check == "keyword_implies_criterion":
            mentioned = any(k in raw_text.lower() for k in rule["keywords"])
            covered = any(c["category"] == rule["required_category"] for c in categorical)
            if mentioned and not covered:
                findings.append(
                    {
                        "rule_id": rule["id"],
                        "severity": "reject",
                        "message": rule["description"],
                    }
                )

    return findings


def run_llm_semantic_review(state: ScreenerState) -> list[dict]:
    # TODO: LLM pass for contradictions deterministic rules can't express
    # (inconsistent age bounds, units mismatched to attributes, criteria
    # present in raw text but missing from the extraction).
    return []


def critic_node(state: ScreenerState) -> dict:
    criteria = state["parsed_criteria"]
    assert criteria is not None, "critic runs after parser — parsed_criteria is set"
    findings = run_deterministic_checks(criteria, state["raw_protocol_text"], load_rules())
    findings += run_llm_semantic_review(state)

    rejects = [f for f in findings if f["severity"] == "reject"]
    passed = not rejects

    return {
        "compliance_passed": passed,
        "compliance_findings": findings,
        "critic_feedback": None
        if passed
        else "\n".join(f"- [{f['rule_id']}] {f['message']}" for f in rejects),
        "current_step": "awaiting_approval" if passed else "parsing",
        "events": [
            event(
                "critic",
                "completed" if passed else "rejected",
                f"{len(findings)} findings ({len(rejects)} blocking)",
            )
        ],
    }


def critic_router(state: ScreenerState) -> str:
    if state["compliance_passed"]:
        return "matcher"
    if state["parse_attempts"] >= get_settings().max_parse_attempts:
        return "human_escalation"
    return "parser"
