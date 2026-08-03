"""Agent 3: Regulatory Critic — hybrid deterministic + LLM compliance guardrail.

Layer 1 runs auditable rule checks from the rules file configured in Settings.
Layer 2 (LLM semantic review of contradictions rules can't express) plugs in
via run_llm_semantic_review. A rejection routes back to the Parser with
structured feedback; after MAX_PARSE_ATTEMPTS (Settings) the graph escalates
to a human instead of looping forever.
"""

import json

import yaml
from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError

from app.config import get_settings
from app.exceptions import DataStoreError, LLMUnavailableError
from app.graph.state import ScreenerState, event
from app.logging_config import get_logger
from app.schemas.review import SemanticReview
from app.services.llm import get_llm, invoke_with_retry
from app.services.metrics import critic_rejections_total

log = get_logger("critic")

CRITIC_SEMANTIC_SYSTEM = """You are a regulatory compliance reviewer performing a \
SECOND-PASS semantic audit of a structured extraction of a clinical trial protocol's \
eligibility criteria. A deterministic rule engine has already run (numeric ranges, \
required fields, vague-language checks) — do NOT repeat those. Focus only on issues \
those rules cannot express:

1. Internal contradictions between criteria — e.g. an inclusion lower age bound and an \
exclusion upper age bound that overlap or conflict (an inclusion "age >= 18" alongside \
an exclusion "age > 65" is an inconsistent way to state an age window and must be \
flagged), the same value both included and excluded, or mutually exclusive requirements.
2. Unit/attribute mismatches — a unit that does not belong to its attribute (e.g. eGFR \
in mmHg, platelets in %, blood pressure in mL/min).
3. Extraction completeness — an eligibility criterion clearly present in the protocol \
text but absent from the structured extraction.

Report ONLY genuine problems. Use severity "reject" for an issue that would corrupt \
patient screening and must be fixed; use "warn" for a concern a human reviewer should \
see but that need not block. If the extraction is sound, return an empty findings list. \
Never invent criteria that are not in the protocol text.

Every finding needs BOTH layers. `message` is for the engineer: name the attribute, the \
operator and the values. `explanation` is for a trial coordinator who does not read \
schemas: one sentence of plain English about the protocol itself, no attribute names, no \
operators, no rule ids."""


# The severity each check kind carries when it fires. A property of the check,
# not of the rule row: a range is either plausible or it is not, while a missing
# age bound is a judgement call for a human — so no rule in the file can pick its
# own. Named here rather than inlined at the four `_finding` calls because the
# rules viewer (#57) has to state a rule's severity without running the engine,
# and a viewer that disagreed with the engine would be worse than no viewer.
CHECK_SEVERITY = {
    "must_be_quantitative": "reject",
    "range": "reject",
    "required_attribute": "warn",
    "keyword_implies_criterion": "reject",
}

# Layer 2's findings are stamped with this in place of a rule id — they come from
# the semantic pass below, which has no row in the rules file. Named so the rules
# viewer can describe that layer under the same id its findings cite (#57).
SEMANTIC_RULE_ID = "LLM-SEM"


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


def _finding(rule: dict, message: str, plain_detail: str = "") -> dict:
    """One finding carrying both layers (#52).

    `message` stays technical — it is what loops back to the Parser as feedback.
    `explanation` is the reviewer-facing sentence: the rule's own `plain` prose
    (falling back to `description` for a rule that has none) plus whatever
    specific the check found, e.g. the implausible number it read.

    Severity is read from `CHECK_SEVERITY` rather than passed in, so the value a
    finding carries and the value the rules viewer publishes are the same lookup.
    """
    plain = rule.get("plain") or rule["description"]
    return {
        "rule_id": rule["id"],
        "severity": CHECK_SEVERITY[rule["check"]],
        "message": message,
        "explanation": f"{plain} {plain_detail}".strip(),
    }


def run_deterministic_checks(criteria: dict, raw_text: str, rules: list[dict]) -> list[dict]:
    findings: list[dict] = []
    quantitative = _all_quantitative(criteria)
    categorical = criteria["inclusion_categorical"] + criteria["exclusion_categorical"]

    for rule in rules:
        check = rule["check"]

        if check == "must_be_quantitative":
            # Only fire when the protocol ITSELF uses the vague language. Keying
            # solely off `unparseable` lets a model that hallucinates, say,
            # "organ function" into that list trigger a phantom rejection for a
            # protocol that never mentions it (observed with small local models).
            in_text = any(k in raw_text.lower() for k in rule["keywords"])
            in_unparseable = any(
                any(k in u.lower() for k in rule["keywords"]) for u in criteria["unparseable"]
            )
            has_quant = any(c["attribute"] == rule["attribute"] for c in quantitative)
            if in_text and in_unparseable and not has_quant:
                findings.append(
                    _finding(
                        rule,
                        f"{rule['description']} — found only vague language, no numeric threshold.",
                    )
                )

        elif check == "range":
            for c in quantitative:
                if c["attribute"] != rule["attribute"]:
                    continue
                if not (rule["min_plausible"] <= c["value"] <= rule["max_plausible"]):
                    findings.append(
                        _finding(
                            rule,
                            f"{rule['description']}: extracted {c['value']} {c['unit']} "
                            f"from '{c['source_text']}'",
                            f"It read {c['value']} {c['unit']} from “{c['source_text']}”.",
                        )
                    )

        elif check == "required_attribute":
            if not any(c["attribute"] == rule["attribute"] for c in quantitative):
                findings.append(_finding(rule, rule["description"]))

        elif check == "keyword_implies_criterion":
            mentioned = any(k in raw_text.lower() for k in rule["keywords"])
            # Covered when the extraction actually contains a criterion ABOUT this
            # topic — matched on the rule's keywords appearing in the criterion
            # text, not on the model tagging it with the exact `category` enum (a
            # weak model may label "pregnant or breastfeeding" as biomarker). This
            # verifies the real intent: the flagged condition was captured at all.
            covered = any(
                any(
                    k in f"{c.get('value', '')} {c.get('source_text', '')}".lower()
                    for k in rule["keywords"]
                )
                for c in categorical
            )
            if mentioned and not covered:
                findings.append(_finding(rule, rule["description"]))

    return findings


def run_llm_semantic_review(state: ScreenerState) -> list[dict]:
    """Layer 2: LLM pass for contradictions the deterministic rules can't express.

    Catches inconsistent bounds, units mismatched to attributes, and criteria
    present in the raw text but missing from the extraction. Findings are stamped
    `rule_id="LLM-SEM"` and merge into the same `compliance_findings` list.

    This is a supplementary safety layer, so a degraded LLM never aborts a
    screening: an unavailable backend yields a single non-blocking `warn`
    (so the gap is visible in the UI, not silent), and malformed output is
    dropped. The deterministic layer still stands on its own.
    """
    criteria = state["parsed_criteria"]
    if criteria is None:
        return []

    structured = get_llm().with_structured_output(SemanticReview)
    prompt = (
        "ORIGINAL PROTOCOL TEXT:\n"
        f"{state['raw_protocol_text']}\n\n"
        "STRUCTURED EXTRACTION (JSON):\n"
        f"{json.dumps(criteria, indent=2)}\n\n"
        "Audit the extraction against the protocol and report any semantic issues."
    )
    messages = [("system", CRITIC_SEMANTIC_SYSTEM), ("user", prompt)]

    try:
        raw = invoke_with_retry(structured, messages)
        review = raw if isinstance(raw, SemanticReview) else SemanticReview.model_validate(raw)
    except LLMUnavailableError as exc:
        log.warning("critic.semantic_review_unavailable", detail=str(exc))
        return [
            {
                "rule_id": SEMANTIC_RULE_ID,
                "severity": "warn",
                "message": "Semantic review skipped — LLM backend unavailable; "
                "deterministic compliance checks only.",
                "explanation": "The second-opinion review of this extraction could not run, so "
                "only the automated rule checks were applied. A reviewer should read the criteria "
                "with that in mind.",
            }
        ]
    except (ValidationError, OutputParserException) as exc:
        log.warning("critic.semantic_review_invalid", error=type(exc).__name__)
        return []

    findings = [
        {
            "rule_id": SEMANTIC_RULE_ID,
            "severity": f.severity,
            "message": f.message,
            # A model that skipped the plain layer still gets a finding — the
            # technical wording is a worse explanation than a purpose-written
            # one, but far better than an empty line in the reviewer's view.
            "explanation": f.explanation.strip() or f.message,
        }
        for f in review.findings
    ]
    log.info("critic.semantic_review", findings=len(findings))
    return findings


def summarize_compliance(findings: list[dict]) -> str:
    """The Critic's result in one plain-language line (#52).

    Non-technical reviewers read this instead of the findings list, so it has to
    answer "can this run proceed, and if not why" without a rule id — while
    still naming the rule that fired, because provenance is never dropped
    (the technical view is one click away, not a different source of truth).

    Derived from the findings alone rather than taking `passed` as well: a
    blocking finding is exactly what makes a run fail (see critic_node), so the
    two can't disagree, and there is no "blocked with nothing to show" state.
    """
    rejects = [f for f in findings if f["severity"] == "reject"]
    warns = [f for f in findings if f["severity"] == "warn"]

    if rejects:
        first = rejects[0]
        lead = f"{first['explanation']} ({first['rule_id']})"
        if len(rejects) == 1:
            return f"❌ Blocked — one problem has to be fixed before screening: {lead}"
        return (
            f"❌ Blocked — {len(rejects)} problems have to be fixed before screening. "
            f"The first: {lead}"
        )

    if not warns:
        return "✅ Cleared — the extraction passed every compliance check; nothing to fix."
    noun = "note" if len(warns) == 1 else "notes"
    return (
        f"✅ Cleared — nothing blocks screening, but there {'is' if len(warns) == 1 else 'are'} "
        f"{len(warns)} advisory {noun} worth reading below."
    )


def critic_node(state: ScreenerState) -> dict:
    criteria = state["parsed_criteria"]
    assert criteria is not None, "critic runs after parser — parsed_criteria is set"
    findings = run_deterministic_checks(criteria, state["raw_protocol_text"], load_rules())
    findings += run_llm_semantic_review(state)

    rejects = [f for f in findings if f["severity"] == "reject"]
    passed = not rejects

    # Count each blocking finding by the rule that fired, so the dashboard shows
    # which rules actually gate screenings (vs. which never trip in production).
    for f in rejects:
        critic_rejections_total.labels(rule_id=f["rule_id"]).inc()

    if passed:
        log.info("critic.passed", findings=len(findings))
    else:
        log.warning(
            "critic.rejected",
            findings=len(findings),
            blocking=len(rejects),
            rule_ids=[f["rule_id"] for f in rejects],
            attempt=state["parse_attempts"],
        )

    return {
        "compliance_passed": passed,
        "compliance_findings": findings,
        "compliance_summary": summarize_compliance(findings),
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
