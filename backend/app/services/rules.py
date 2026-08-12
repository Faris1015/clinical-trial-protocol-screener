"""The compliance rules database — readable by reviewers (#57), authored by admins (#97).

`app/rules/compliance_rules.yaml` is layer 1 of the Regulatory Critic: the rows
that decide whether an extraction is fit to screen patients against. #57 turned
those rows into a listing, so "why did RENAL-001 stop my protocol" became a
question the app answers. This module now also owns the other half — changing
them without a redeploy.

Six decisions worth knowing before editing:

**The file seeds; the table rules.** The YAML populates `compliance_rules` on
first boot and stays in the repo as the documented default set, but from the
moment that table has a row it is the source of truth and the file is never read
again (`seed` is a no-op against a non-empty table). Anything else would make a
redeploy silently revert an admin's work — the exact failure the issue exists to
remove. `RULES_PATH` therefore configures *what a fresh instance starts with*,
not what a running one enforces, and `list_compliance_rules` says so.

**Validated on write, never mid-screening.** `run_deterministic_checks` indexes
straight into a rule (`rule["min_plausible"]`), so a malformed row is a KeyError
inside somebody's run. `validate` is what stops that: every rule is checked
against its check kind's contract at authoring time, and a rule the engine could
not run — or could run only to no effect — is a 422 for the admin who typed it,
while the form is still on screen.

**Retirement is soft, and that is a correctness property.** A finding cites a
rule id forever; a run checkpointed last quarter still carries `RENAL-001`. So a
retired rule keeps its row and loses only its `enabled` flag: the engine stops
running it, the viewer keeps rendering it (as retired), and every past finding
still resolves. There is no delete, on purpose.

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

**Every mutation is attributed and indexed.** A rule carries who created it and
who last revised it, and each change is appended to the org-wide decision index
(#98) as a `rule` subject. An admin quietly widening a plausible range is exactly
the kind of change an auditor needs to find later, and the rules table alone only
ever shows the *current* wording.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.exceptions import DuplicateRuleError, InvalidRuleError, RuleNotFoundError
from app.graph.nodes.critic import CHECK_SEVERITY, SEMANTIC_RULE_ID, load_rules
from app.logging_config import get_logger
from app.persistence import SUBJECT_RULE, AuditStore, RuleRecord, RuleStore
from app.services import audit

if TYPE_CHECKING:
    from app.auth import Principal

log = get_logger("rules")

# What each check kind is called on screen. The raw value travels too (`check`),
# so a client can group or filter on the kind without matching on prose.
_CHECK_LABELS = {
    "must_be_quantitative": "Numeric threshold required",
    "range": "Plausible range",
    "required_attribute": "Required criterion",
    "keyword_implies_criterion": "Implied criterion",
}

# The check kinds an author may pick. Exactly the branches
# `run_deterministic_checks` implements — a rule of any other kind would sit in
# the table looking live and never fire, which is the one failure a rules engine
# must not have. Derived from `CHECK_SEVERITY` rather than typed out again so a
# fifth check kind cannot be added to the engine and forgotten here.
CHECK_KINDS: tuple[str, ...] = tuple(CHECK_SEVERITY)

# Who the seed rows are attributed to. Not a person and not empty: an empty
# `created_by` reads as missing data in the viewer, and anything email-shaped
# could collide with a configured account (`parse_users` requires an `@`, so this
# string can never be one — the same reasoning as `audit.SYSTEM_ACTOR`).
SEED_ACTOR = "trialgate-seed"

# The actions a rule mutation appends to the decision index live in
# `services/audit.ACTIONS`, with every other action the index carries — the
# filter validates against that one tuple, so a second copy here would be a
# second thing to keep in step. Named for their subject ("rule_created", not
# "created") so an auditor reads "Rule created" beside "Approved".

# An id has to survive being embedded in a finding, a URL (`/rules?rule=<id>`)
# and a CSV cell, and it is quoted back by auditors years later — so it is a
# short, boring token rather than free text. Uppercase by convention (every
# seeded id is), digits and separators allowed.
_MAX_ID_CHARS = 64
_MAX_TEXT_CHARS = 1000

# The one id an author may never take: layer 2's findings are stamped with it,
# and a deterministic rule wearing the same id would make a semantic finding
# resolve to a rule the model never ran.
_RESERVED_IDS = frozenset({SEMANTIC_RULE_ID})

# The semantic layer's severity is whatever the model assigned the finding, so it
# is the one entry that cannot state one up front.
_VARIES = "varies"

# Layer 2 (`critic.run_llm_semantic_review`) has no row in the rules table, but
# its findings cite a rule id like any other and a reviewer who follows that link
# has to land on something. Described here rather than seeded into the table
# because it is not a rule the deterministic engine can run — a row for it would
# make `run_deterministic_checks` iterate something it must ignore, and would let
# an admin "retire" a layer they cannot actually turn off.
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
    # Not authored, so not editable and not retirable. The flags travel rather
    # than being inferred from `layer` in the client: a component deciding for
    # itself which rows carry an edit button is a component that will eventually
    # offer one for this.
    "enabled": True,
    "editable": False,
    "created_by": "",
    "created_at": "",
    "updated_by": "",
    "updated_at": "",
}


def _text(value: Any) -> str:
    """A stored scalar as a string, with anything unexpected flattened to empty.

    The seed file is operator-editable and `RULES_PATH` can point at a
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


def _condition(check: str, params: dict[str, Any], attribute: str) -> str:
    """The rule's threshold/operator in one line, per check kind.

    This is the column #57 asked for: what the rule actually tests, stated so a
    reviewer can check it against their protocol without reading YAML.
    """
    subject = attribute or "the extraction"

    if check == "range":
        low = _number(params.get("min_plausible"))
        high = _number(params.get("max_plausible"))
        return f"{low} ≤ {subject} ≤ {high}"
    if check == "must_be_quantitative":
        return f"{subject} must be a numeric threshold, not qualitative language"
    if check == "required_attribute":
        return f"{subject} must be present among the extracted criteria"
    if check == "keyword_implies_criterion":
        category = _text(params.get("required_category")) or "matching"
        return f"a {category} criterion must be extracted when the protocol raises the topic"
    # A check kind the engine has no branch for never fires — `run_deterministic_checks`
    # falls off the end of its if/elif chain. `validate` rejects these on write, so
    # only a hand-edited database reaches this line; say so rather than inventing a
    # condition for a rule that does nothing.
    return "Not implemented by the rule engine — this rule never fires."


# --- Validation (AC 3) ------------------------------------------------------


def _require_text(value: Any, field: str, *, max_chars: int = _MAX_TEXT_CHARS) -> str:
    """A required free-text field, or a 422 naming it.

    `field` carries its own article ("an id", "a description") — these messages
    are read by the admin filling in the form, and "A rule needs a id." is the
    kind of sentence that makes a careful person doubt the rest of the page.
    """
    text = _text(value)
    if not text:
        raise InvalidRuleError(f"A rule needs {field}.")
    if len(text) > max_chars:
        raise InvalidRuleError(f"{field.capitalize()} must be {max_chars} characters or fewer.")
    return text


def _validate_id(raw: Any) -> str:
    """The rule's id, or a 422 explaining which way it is unusable."""
    rule_id = _require_text(raw, "an id", max_chars=_MAX_ID_CHARS)
    if rule_id.upper() in _RESERVED_IDS:
        raise InvalidRuleError(
            f"{rule_id!r} is reserved for the Critic's semantic layer — pick another id."
        )
    # Anchored, and checked character by character rather than by regex so the
    # message can name what is wrong. `-` and `_` only: an id travels in a query
    # string and a CSV cell, and the seeded ids (RENAL-001) set the convention.
    if not all(c.isalnum() or c in "-_" for c in rule_id):
        raise InvalidRuleError(
            f"Rule id {rule_id!r} may use only letters, digits, hyphens and underscores."
        )
    if not rule_id[0].isalnum():
        raise InvalidRuleError(f"Rule id {rule_id!r} must start with a letter or a digit.")
    return rule_id


def _validate_keywords(raw: Any, *, check: str) -> list[str]:
    """The rule's keyword list, normalized to lowercase.

    Lowercased on write because that is how the engine compares them — it lowers
    the protocol text and the criterion text, never the keyword. A rule authored
    with "Renal" would otherwise silently never match, which is the worst kind of
    broken rule: one that looks live and quietly passes everything.

    `isinstance` before iterating, not just truthiness: `keywords: renal` in a
    hand-edited seed file is a string, and iterating a string yields its
    characters — five one-letter "keywords" rather than an obviously wrong entry.
    """
    if raw is None:
        keywords: list[str] = []
    elif isinstance(raw, (list, tuple)):
        keywords = [k for k in (_text(item).lower() for item in raw) if k]
    else:
        raise InvalidRuleError("A rule's keywords must be a list of words.")

    # The two kinds that are *brought into scope* by protocol wording. Without a
    # keyword neither can ever fire, so an empty list is a rule that does nothing
    # — refused on write rather than stored as a no-op an admin believes is live.
    if check in ("must_be_quantitative", "keyword_implies_criterion") and not keywords:
        raise InvalidRuleError(
            f"A {check!r} rule needs at least one keyword — it is what brings the rule "
            "into play for a protocol."
        )
    return keywords


def _validate_params(payload: dict[str, Any], check: str) -> dict[str, Any]:
    """The check kind's own required fields, and only those."""
    if check != "range":
        if check == "keyword_implies_criterion":
            return {
                "required_category": _require_text(
                    payload.get("required_category"), "a required_category"
                )
            }
        return {}

    bounds: dict[str, Any] = {}
    for key in ("min_plausible", "max_plausible"):
        value = payload.get(key)
        # `bool` first: `True` is an `int` in Python, and a range of True..200
        # would pass a bare isinstance check and then compare as 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidRuleError(f"A 'range' rule needs a numeric {key}.")
        bounds[key] = value
    if bounds["min_plausible"] >= bounds["max_plausible"]:
        raise InvalidRuleError(
            "A 'range' rule needs min_plausible below max_plausible; "
            f"got {bounds['min_plausible']} and {bounds['max_plausible']}."
        )
    return bounds


def validate(payload: dict[str, Any], *, rule_id: str | None = None) -> dict[str, Any]:
    """Check one authored rule against its check kind's contract (AC 3).

    Returns the normalized authored fields — the shape `RuleRecord` stores —
    or raises `InvalidRuleError` naming the first thing wrong. `rule_id` is
    supplied on an edit, where the id comes from the URL and is not the author's
    to change: a rule id is what findings cite, so re-pointing one at different
    wording is a new rule, not an edit.

    Validation is total rather than best-effort. Every field the engine reads is
    checked here, so `run_deterministic_checks` can keep indexing directly into a
    rule without a defensive `.get` at each branch.
    """
    check = _text(payload.get("check"))
    if check not in CHECK_KINDS:
        raise InvalidRuleError(
            f"Unknown check kind {check!r}; expected one of {', '.join(CHECK_KINDS)}."
        )

    attribute = _text(payload.get("attribute"))
    # Three of the four kinds test a named attribute and read `rule["attribute"]`
    # to find it. `keyword_implies_criterion` is the exception: it asks whether
    # *any* criterion covers a topic, so it has no single attribute to name.
    if check != "keyword_implies_criterion" and not attribute:
        raise InvalidRuleError(f"A {check!r} rule needs the attribute it applies to.")

    return {
        "id": _validate_id(rule_id if rule_id is not None else payload.get("id")),
        "check": check,
        "attribute": attribute,
        "description": _require_text(payload.get("description"), "a description"),
        # Optional by design (#52): a rule without plain prose falls back to its
        # description, so a new rule is never blocked on writing two versions.
        "plain": _text(payload.get("plain"))[:_MAX_TEXT_CHARS],
        "keywords": _validate_keywords(payload.get("keywords"), check=check),
        "params": _validate_params(payload, check),
    }


# --- Seeding (AC 1) ---------------------------------------------------------


async def seed_from_file(store: RuleStore) -> int:
    """Populate an empty rules table from the YAML the repo ships. Returns rows written.

    Runs on every boot and does nothing on all but the first: `RuleStore.seed`
    checks emptiness inside the same call, so a redeploy can never revert an
    admin's edits. See the module docstring.

    A malformed row in the seed file is *skipped with a warning* rather than
    raising. This runs in the app's lifespan, and a rules file with one bad row
    should not stop the server from starting — the remaining rules still guard
    every screening, and the admin can fix the row through the API the moment the
    app is up. A file that is missing or entirely unreadable is a different thing
    and still raises `DataStoreError` from `load_rules`.
    """
    existing = await store.list()
    if existing:
        return 0

    stamped = datetime.now(UTC).isoformat()
    records: list[RuleRecord] = []
    for position, raw in enumerate(load_rules()):
        if not isinstance(raw, dict):
            log.warning("rules.seed_row_skipped", reason="not a mapping", position=position)
            continue
        try:
            fields = validate(raw)
        except InvalidRuleError as exc:
            log.warning(
                "rules.seed_row_skipped",
                reason=str(exc),
                position=position,
                rule_id=_text(raw.get("id")) or None,
            )
            continue
        records.append(
            RuleRecord(
                **fields,
                position=position,
                enabled=True,
                created_by=SEED_ACTOR,
                created_at=stamped,
                updated_by=SEED_ACTOR,
                updated_at=stamped,
            )
        )

    written = await store.seed(records)
    if written:
        log.info("rules.seeded", rules=written, source=get_settings().rules_path.name)
    return written


# --- Reading ----------------------------------------------------------------


def _present(record: RuleRecord) -> dict[str, Any]:
    """One stored rule as the payload every reader of this listing gets."""
    return {
        # The bounds and category as stored, so an editor can populate its form
        # from the listing rather than needing a second per-rule fetch.
        #
        # Spread *first*, so the fields below always win. `params` is a JSON blob
        # from a column an operator can reach with a SQL client, and a row whose
        # params held `{"id": ...}` or `{"severity": "warn"}` would otherwise
        # rewrite the very fields this page exists to state authoritatively —
        # publishing a severity the engine does not apply, which is the one thing
        # a rules viewer must never do.
        **record.params,
        "id": record.id,
        "attribute": record.attribute,
        "check": record.check,
        "check_label": _CHECK_LABELS.get(record.check, record.check or "Unknown check"),
        "condition": _condition(record.check, record.params, record.attribute),
        # An unrunnable check has no severity to publish — the rule cannot
        # produce a finding at all, and "reject" would be a lie about a no-op.
        "severity": CHECK_SEVERITY.get(record.check, ""),
        "description": record.description,
        # The same fallback `critic._finding` applies, so the prose on the rules
        # page is the prose a finding from that rule would carry.
        "plain": record.plain or record.description,
        "keywords": list(record.keywords),
        "layer": "deterministic",
        # Retired rules are listed, not hidden (AC 4) — a finding that cites one
        # still has to resolve. `enabled` is what lets the viewer say "retired"
        # instead of pretending the rule is still guarding screenings.
        "enabled": record.enabled,
        "editable": True,
        "created_by": record.created_by,
        "created_at": record.created_at,
        "updated_by": record.updated_by,
        "updated_at": record.updated_at,
    }


async def list_compliance_rules(store: RuleStore) -> dict[str, Any]:
    """Every rule the Critic checks a protocol against, retired ones included.

    Ordered by `position` — the seed file groups rules by clinical domain and
    comments each group, which is the order the person who maintains them thinks
    in, and authored rules append to it.

    `source` is the file the table was *seeded* from, not the file it runs: since
    first boot the table is the source of truth (see the module docstring). It is
    reported because an operator comparing two instances needs to know which
    default set each started from — the filename only, since the absolute path is
    server topology and this payload goes to every signed-in reviewer's browser.
    """
    records = await store.list()
    rules = [_present(record) for record in records]
    rules.append(_SEMANTIC_RULE)
    return {
        "rules": rules,
        "source": get_settings().rules_path.name,
        # What the engine will actually run on the next screening. Derived here
        # rather than counted in the browser so the figure agrees with
        # `active_engine_rules` by construction.
        "active": sum(1 for record in records if record.enabled),
    }


async def active_engine_rules(store: RuleStore) -> list[dict[str, Any]]:
    """The enabled rules, in the shape `run_deterministic_checks` reads.

    The one path between the table and the engine. Disabled rules are filtered in
    SQL rather than skipped inside the check loop, so a retired rule cannot fire
    through a branch that forgot to look at the flag.
    """
    records = await store.list(include_disabled=False)
    return [record.as_engine_rule() for record in records]


# --- Authoring (AC 2, AC 5) -------------------------------------------------


def _summarize(fields: dict[str, Any]) -> str:
    """The one line about a rule that lands in the decision index.

    The check kind and the condition rather than the description: an auditor
    scanning the index wants to see *what the rule now enforces*, and the
    condition is the rendered form of exactly that.
    """
    condition = _condition(fields["check"], fields["params"], fields["attribute"])
    return f"{fields['check']} — {condition}"


async def _record_mutation(
    audit_store: AuditStore,
    action: str,
    rule_id: str,
    *,
    actor: Principal,
    detail: str,
    occurred_at: str,
) -> None:
    """Append one rule change to the org-wide decision index (AC 5).

    Subject `rule` rather than `screening`: this decision is about no run, and
    `thread_id` is left empty rather than filled with something run-shaped that
    would deep-link an auditor to a 404. See `persistence.AuditDecision`.
    """
    await audit.record(
        audit_store,
        action,
        "",
        actor=actor,
        detail=detail,
        occurred_at=occurred_at,
        subject_kind=SUBJECT_RULE,
        subject_id=rule_id,
    )


async def create_rule(
    store: RuleStore,
    audit_store: AuditStore,
    payload: dict[str, Any],
    actor: Principal,
) -> dict[str, Any]:
    """Author a new rule. It guards the next screening, not the ones in flight."""
    fields = validate(payload)
    if await store.get(fields["id"]) is not None:
        raise DuplicateRuleError(
            f"A rule with id {fields['id']!r} already exists. Edit that rule, or pick another id."
        )

    stamped = datetime.now(UTC).isoformat()
    record = RuleRecord(
        **fields,
        position=await store.next_position(),
        enabled=True,
        created_by=actor.email,
        created_at=stamped,
        updated_by=actor.email,
        updated_at=stamped,
    )
    await store.add(record)
    log.info("rules.created", rule_id=record.id, actor=actor.email, check=record.check)
    await _record_mutation(
        audit_store,
        "rule_created",
        record.id,
        actor=actor,
        detail=_summarize(fields),
        occurred_at=stamped,
    )
    return _present(record)


async def update_rule(
    store: RuleStore,
    audit_store: AuditStore,
    rule_id: str,
    payload: dict[str, Any],
    actor: Principal,
) -> dict[str, Any]:
    """Revise an existing rule's wording, thresholds or scope.

    A full replacement of the authored fields rather than a sparse patch: a rule's
    fields are interdependent (changing `check` changes which of them are even
    required), so applying a subset would let a `range` rule keep bounds it no
    longer uses, or lose the ones it now needs. The editor sends the whole rule
    back, which is also what makes `validate` able to check it as a whole.

    `enabled` is not revised here — retirement is its own endpoint, because it is
    the one change an auditor looks for specifically and burying it in a general
    edit would make it invisible in the index.
    """
    existing = await store.get(rule_id)
    if existing is None:
        raise RuleNotFoundError(f"No compliance rule with id {rule_id!r}.")

    fields = validate(payload, rule_id=rule_id)
    stamped = datetime.now(UTC).isoformat()
    record = RuleRecord(
        **fields,
        position=existing.position,
        enabled=existing.enabled,
        created_by=existing.created_by,
        created_at=existing.created_at,
        updated_by=actor.email,
        updated_at=stamped,
    )
    await store.replace(record)
    log.info("rules.updated", rule_id=rule_id, actor=actor.email, check=record.check)
    await _record_mutation(
        audit_store,
        "rule_updated",
        rule_id,
        actor=actor,
        detail=_summarize(fields),
        occurred_at=stamped,
    )
    return _present(record)


async def set_rule_enabled(
    store: RuleStore,
    audit_store: AuditStore,
    rule_id: str,
    enabled: bool,
    actor: Principal,
) -> dict[str, Any]:
    """Retire a rule, or bring a retired one back (AC 4).

    Soft in both directions: the row stays, so every finding that ever cited this
    id keeps resolving and the viewer keeps listing it. Only the engine's view
    changes, from the next screening onward.
    """
    existing = await store.get(rule_id)
    if existing is None:
        raise RuleNotFoundError(f"No compliance rule with id {rule_id!r}.")

    stamped = datetime.now(UTC).isoformat()
    await store.set_enabled(rule_id, enabled, actor.email, stamped)
    log.info("rules.enabled_changed", rule_id=rule_id, enabled=enabled, actor=actor.email)
    await _record_mutation(
        audit_store,
        "rule_enabled" if enabled else "rule_disabled",
        rule_id,
        actor=actor,
        detail=(
            "Rule restored — it guards screenings again from the next run."
            if enabled
            else "Rule retired — it stops firing, and past findings still resolve to it."
        ),
        occurred_at=stamped,
    )
    # Re-read rather than reconstruct: the store owns what a write actually
    # persisted, and a payload assembled from the pre-write record would report
    # the old `updated_by` back to the admin who just changed it.
    updated = await store.get(rule_id)
    assert updated is not None  # just written, in the same connection
    return _present(updated)
