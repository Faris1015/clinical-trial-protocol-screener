"""The compliance rules database, as something a reviewer can read (#57).

`app/rules/compliance_rules.yaml` is the Critic's deterministic layer: eight rows
that decide whether an extraction is fit to screen patients against. Until now
the only way to see one was to be blocked by it — a finding names a rule id, and
the id meant nothing unless you had the file open. This module turns those rows
into a listing, so "why did RENAL-001 stop my protocol" is a question the app
answers.

Two decisions worth knowing before editing:

**Rendered here, not in the browser.** Each rule leaves with its threshold
already written out ("90 ≤ systolic_bp ≤ 200") and its check kind already named,
the same convention `services/timeline.py` follows. The rendering depends on the
check kind — a range reads nothing like a required-attribute rule — and that
knowledge belongs beside the engine that runs them, not in a component that would
have to re-derive it and could drift.

**Derived from the engine, never restated.** Severity comes from
`critic.CHECK_SEVERITY`, so a rule's published severity is the same lookup the
Critic performs when it fires. A viewer that told a reviewer "advisory" for a rule
that actually blocks the run would be worse than no viewer at all — this is an
audit surface, and its whole value is that it agrees with what ran.

Read-only by design: an admin editor is a follow-up, and a rules file that the
app can rewrite needs a change trail of its own before it can be trusted.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.graph.nodes.critic import CHECK_SEVERITY, SEMANTIC_RULE_ID, load_rules
from app.logging_config import get_logger

log = get_logger("rules")

# What each check kind is called on screen. The raw value travels too (`check`),
# so a client can group or filter on the kind without matching on prose.
_CHECK_LABELS = {
    "must_be_quantitative": "Numeric threshold required",
    "range": "Plausible range",
    "required_attribute": "Required criterion",
    "keyword_implies_criterion": "Implied criterion",
}

# The semantic layer's severity is whatever the model assigned the finding, so it
# is the one entry that cannot state one up front.
_VARIES = "varies"

# Layer 2 (`critic.run_llm_semantic_review`) has no row in the rules file, but its
# findings cite a rule id like any other and a reviewer who follows that link has
# to land on something. Described here rather than added to the YAML because it
# is not a rule the deterministic engine can run — putting it in the file would
# make `run_deterministic_checks` iterate a row it must ignore.
_SEMANTIC_RULE: dict[str, Any] = {
    "id": SEMANTIC_RULE_ID,
    "attribute": "",
    "check": "llm_semantic_review",
    "check_label": "Semantic review",
    "condition": (
        "A second-pass model review for contradictions between criteria, units that do not "
        "belong to their attribute, and criteria present in the protocol but missing from "
        "the extraction."
    ),
    "severity": _VARIES,
    "description": (
        "Layer 2 of the Critic: an LLM audit of the extraction against the protocol text, "
        "covering the issues the deterministic rules cannot express. Findings carry the "
        "severity the review assigned them; an unavailable backend yields a single "
        "non-blocking warning rather than skipping silently."
    ),
    "plain": (
        "A second opinion on what was read out of the protocol — it looks for criteria that "
        "contradict each other, measurements in the wrong units, and requirements the "
        "protocol states but the extraction missed."
    ),
    "keywords": [],
    "layer": "semantic",
}


def _text(value: Any) -> str:
    """A YAML scalar as a string, with anything unexpected flattened to empty.

    The rules file is operator-editable and `RULES_PATH` can point at a
    hand-written one, so a row here may hold a list where prose was meant. A
    listing that renders that as an empty cell is recoverable; one that raises
    takes down the page that would have shown the operator their mistake.
    """
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> str:
    """A bound as it was written — `10` stays `10`, not `10.0`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return str(value)


def _condition(rule: dict[str, Any], attribute: str) -> str:
    """The rule's threshold/operator in one line, per check kind.

    This is the column the issue asks for: what the rule actually tests, stated
    so a reviewer can check it against their protocol without reading YAML.
    """
    check = _text(rule.get("check"))
    subject = attribute or "the extraction"

    if check == "range":
        low = _number(rule.get("min_plausible"))
        high = _number(rule.get("max_plausible"))
        return f"{low} ≤ {subject} ≤ {high}"
    if check == "must_be_quantitative":
        return f"{subject} must be a numeric threshold, not qualitative language"
    if check == "required_attribute":
        return f"{subject} must be present among the extracted criteria"
    if check == "keyword_implies_criterion":
        category = _text(rule.get("required_category")) or "matching"
        return f"a {category} criterion must be extracted when the protocol raises the topic"
    # A check kind the engine has no branch for never fires — `run_deterministic_checks`
    # falls off the end of its if/elif chain. Say so rather than inventing a
    # condition for a rule that does nothing.
    return "Not implemented by the rule engine — this rule never fires."


def _present(rule: Any) -> dict[str, Any] | None:
    """One YAML row as a listing entry, or None if it isn't one.

    A row without an id is dropped: the id is what a finding cites, so an
    unidentified rule is one nothing can ever link to.
    """
    if not isinstance(rule, dict):
        return None
    rule_id = _text(rule.get("id"))
    if not rule_id:
        return None

    check = _text(rule.get("check"))
    attribute = _text(rule.get("attribute"))
    description = _text(rule.get("description"))
    # `isinstance` before iterating, not just truthiness: `keywords: renal` in a
    # hand-edited file is a string, and iterating a string yields its characters —
    # five one-letter "keywords" rather than an obviously wrong single entry.
    raw_keywords = rule.get("keywords")
    keywords = (
        [k for k in (_text(k) for k in raw_keywords) if k]
        if isinstance(raw_keywords, (list, tuple))
        else []
    )

    return {
        "id": rule_id,
        "attribute": attribute,
        "check": check,
        "check_label": _CHECK_LABELS.get(check, check or "Unknown check"),
        "condition": _condition(rule, attribute),
        # An unrunnable check has no severity to publish — the rule cannot
        # produce a finding at all, and "reject" would be a lie about a no-op.
        "severity": CHECK_SEVERITY.get(check, ""),
        "description": description,
        # The same fallback `critic._finding` applies, so the prose on the rules
        # page is the prose a finding from that rule would carry.
        "plain": _text(rule.get("plain")) or description,
        "keywords": keywords,
        "layer": "deterministic",
    }


def list_compliance_rules() -> dict[str, Any]:
    """Every rule the Critic checks a protocol against, in file order.

    File order is kept rather than sorted by id: the file groups rules by clinical
    domain and comments the group above it, which is the order the person who
    maintains it thinks in.

    A missing or malformed file raises `DataStoreError` (503) from `load_rules` —
    the same failure the Critic itself would hit on the next run, surfaced on a
    page instead of mid-screening.
    """
    raw = load_rules()
    rules = [entry for entry in (_present(rule) for rule in raw) if entry]
    dropped = len(raw) - len(rules)
    if dropped:
        # Not an error: the engine skips these rows too. But a rule an operator
        # believes is live and that no one can see is worth a line in the log.
        log.warning("rules.unlistable_rows", dropped=dropped, listed=len(rules))
    rules.append(_SEMANTIC_RULE)
    # The filename only — the absolute path is server topology, and this payload
    # goes to every signed-in reviewer's browser.
    return {"rules": rules, "source": get_settings().rules_path.name}
