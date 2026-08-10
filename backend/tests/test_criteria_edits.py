"""The before/after diff behind edit-and-rerun (#53).

`diff_criteria` is what makes a reviewer's correction auditable, so these pin the
two things that are easy to get subtly wrong: criteria are paired by provenance
(not by list position, which shifts under a delete), and a sentence that moves out
of `unparseable` into a real bucket reads as one reclassification rather than an
unexplained delete plus an unexplained add.
"""

from __future__ import annotations

from app.graph.nodes.critic import load_rules, run_deterministic_checks
from app.services.criteria_edits import diff_criteria, edit_record, summarize
from tests.auth_helpers import REVIEWER


def _quant(value: float, source: str, attribute: str = "age", unit: str = "years") -> dict:
    return {
        "attribute": attribute,
        "operator": ">=",
        "value": value,
        "value_high": None,
        "unit": unit,
        "source_text": source,
    }


def _cat(value: str, source: str, negated: bool = False) -> dict:
    return {
        "category": "diagnosis",
        "value": value,
        "negated": negated,
        "source_text": source,
    }


def _criteria(**buckets: object) -> dict:
    base: dict = {
        "trial_title": "T",
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    base.update(buckets)
    return base


def test_unchanged_extraction_has_no_changes():
    criteria = _criteria(inclusion_quantitative=[_quant(18, "Age 18 or older.")])
    assert diff_criteria(criteria, criteria) == []


def test_edited_threshold_is_one_modification():
    before = _criteria(inclusion_quantitative=[_quant(180, "Age 18 or older.")])
    after = _criteria(inclusion_quantitative=[_quant(18, "Age 18 or older.")])
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "modified"
    assert change["bucket"] == "inclusion_quantitative"
    assert change["before"] == "age >= 180 years"
    assert change["after"] == "age >= 18 years"
    assert change["from_bucket"] is None


def test_deleted_criterion_does_not_report_its_survivors_as_changed():
    """The regression an index-wise diff would produce: deleting the first of
    three criteria shifts the rest, and a positional comparison would call every
    one of them modified."""
    keep_a = _quant(18, "Age 18 or older.")
    keep_b = _quant(30, "eGFR at least 30.", attribute="egfr", unit="mL/min/1.73m2")
    hallucinated = _quant(99, "Nothing in the protocol says this.")
    before = _criteria(inclusion_quantitative=[hallucinated, keep_a, keep_b])
    after = _criteria(inclusion_quantitative=[keep_a, keep_b])

    (change,) = diff_criteria(before, after)
    assert change["kind"] == "removed"
    assert change["before"] == "age >= 99 years"
    assert change["after"] is None


def test_reclassified_unparseable_pairs_into_one_change():
    sentence = "Adequate bone marrow function."
    before = _criteria(unparseable=[sentence])
    after = _criteria(
        inclusion_quantitative=[
            _quant(1.5, sentence, attribute="anc", unit="10^9/L"),
        ]
    )
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "reclassified"
    assert change["from_bucket"] == "unparseable"
    assert change["bucket"] == "inclusion_quantitative"
    assert change["before"] == sentence
    assert change["after"] == "anc >= 1.5 10^9/L"


def test_demoting_a_mis_parsed_criterion_pairs_into_one_change():
    """The promotion run backwards (#92): a criterion the Parser typed but got
    wrong goes back to being its sentence.

    Same pairing rule, so the reviewer who undoes a bad promotion — or catches a
    hallucinated threshold on a sentence that was never numeric — gets one
    reclassification naming both ends, not a delete whose replacement in
    `unparseable` looks unrelated to it."""
    sentence = "Adequate hepatic function."
    before = _criteria(
        inclusion_quantitative=[_quant(60, sentence, attribute="egfr", unit="mL/min/1.73m2")]
    )
    after = _criteria(unparseable=[sentence])
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "reclassified"
    assert change["from_bucket"] == "inclusion_quantitative"
    assert change["bucket"] == "unparseable"
    assert change["before"] == "egfr >= 60 mL/min/1.73m2"
    assert change["after"] == sentence


def test_demoting_an_organ_function_criterion_re_arms_the_critic():
    """The consequence of a demotion, pinned so it stays deliberate (#92).

    `must_be_quantitative` fires on exactly the state a demotion creates — the
    protocol states the requirement in prose, the extraction now has it only in
    `unparseable`, and no numeric threshold covers the attribute. So sending a
    mis-parsed organ-function criterion back is *supposed* to re-reject the run:
    the extraction really cannot screen something the protocol asks for, and
    hiding that would make the demotion a way to launder an unscreenable protocol
    past the guardrail. The reviewer's exit is #91's reject, not a quiet demotion.
    """
    text = "Patients must have adequate hepatic function and adequate organ function."
    sentence = "Patients must have adequate hepatic function."
    rules = load_rules()

    def blocking(criteria: dict) -> set[str]:
        findings = run_deterministic_checks(criteria, text, rules)
        return {f["rule_id"] for f in findings if f["severity"] == "reject"}

    typed = _criteria(
        inclusion_quantitative=[_quant(1.2, sentence, attribute="creatinine", unit="mg/dL")]
    )
    assert blocking(typed) == set()
    # Deleting it outright does not trip the rule — nothing lands in `unparseable`
    # — which is the asymmetry that makes demotion the more honest of the two.
    assert blocking(_criteria()) == set()
    assert blocking(_criteria(unparseable=[sentence])) == {"HEPATIC-001"}


def test_a_promotion_and_a_demotion_in_one_revision_do_not_cross_pair():
    """Both directions in the same edit — the realistic revision, since a reviewer
    who notices the Parser mis-typed one sentence usually rescues another in the
    same pass.

    The reclassification pass matches a removal to an addition on provenance
    alone, so with one criterion leaving `unparseable` and another arriving in it
    the two must pair with their own sentences and not with each other."""
    rescued = "Absolute neutrophil count at least 1.5 x 10^9/L."
    mis_parsed = "Adequate hepatic function."
    before = _criteria(
        unparseable=[rescued],
        inclusion_quantitative=[_quant(60, mis_parsed, attribute="egfr", unit="mL/min/1.73m2")],
    )
    after = _criteria(
        unparseable=[mis_parsed],
        inclusion_quantitative=[_quant(1.5, rescued, attribute="anc", unit="10^9/L")],
    )

    changes = diff_criteria(before, after)
    assert [c["kind"] for c in changes] == ["reclassified", "reclassified"]
    by_destination = {c["bucket"]: c for c in changes}
    assert by_destination["inclusion_quantitative"]["before"] == rescued
    assert by_destination["inclusion_quantitative"]["after"] == "anc >= 1.5 10^9/L"
    assert by_destination["unparseable"]["before"] == "egfr >= 60 mL/min/1.73m2"
    assert by_destination["unparseable"]["after"] == mis_parsed


def test_moving_a_criterion_between_buckets_is_a_reclassification():
    """Inclusion→exclusion is the other reclassification a reviewer makes, and it
    must not read as a delete plus an unrelated add."""
    source = "Patients with prior platinum chemotherapy are excluded."
    before = _criteria(inclusion_categorical=[_cat("prior platinum", source)])
    after = _criteria(exclusion_categorical=[_cat("prior platinum", source)])
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "reclassified"
    assert (change["from_bucket"], change["bucket"]) == (
        "inclusion_categorical",
        "exclusion_categorical",
    )


def test_added_criterion_reports_only_an_addition():
    before = _criteria()
    after = _criteria(exclusion_categorical=[_cat("pregnancy", "Pregnant or breastfeeding.")])
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "added"
    assert change["before"] is None
    assert change["after"] == "pregnancy (diagnosis)"


def test_negation_flip_is_visible_in_the_labels():
    source = "No active infection."
    before = _criteria(inclusion_categorical=[_cat("active infection", source)])
    after = _criteria(inclusion_categorical=[_cat("active infection", source, negated=True)])
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "modified"
    assert change["before"] == "active infection (diagnosis)"
    assert change["after"] == "NOT active infection (diagnosis)"


def test_between_operator_renders_both_bounds():
    source = "eGFR 30 to 60."
    ranged = {
        "attribute": "egfr",
        "operator": "between",
        "value": 30,
        "value_high": 60,
        "unit": "mL/min/1.73m2",
        "source_text": source,
    }
    before = _criteria(inclusion_quantitative=[_quant(30, source, attribute="egfr")])
    after = _criteria(inclusion_quantitative=[ranged])
    (change,) = diff_criteria(before, after)
    assert change["after"] == "egfr between 30–60 mL/min/1.73m2"


def test_whole_number_thresholds_render_without_a_decimal():
    """`value` is a float on the wire, so an unformatted label would read
    "age >= 18.0 years" and leave a reviewer wondering what the .0 means. A
    genuinely fractional bound still keeps its decimal."""
    source = "Age 18 or older."
    before = _criteria(inclusion_quantitative=[_quant(18.0, source)])
    after = _criteria(inclusion_quantitative=[_quant(1.5, source, attribute="anc", unit="10^9/L")])
    (change,) = diff_criteria(before, after)
    assert change["before"] == "age >= 18 years"
    assert change["after"] == "anc >= 1.5 10^9/L"


def test_duplicate_provenance_pairs_one_to_one():
    """Two criteria quoting the same sentence must not both match the first one —
    otherwise editing one of them reports a phantom change to the other."""
    source = "Age between 18 and 75."
    before = _criteria(
        inclusion_quantitative=[_quant(18, source), _quant(75, source)],
    )
    after = _criteria(
        inclusion_quantitative=[_quant(18, source), _quant(80, source)],
    )
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "modified"
    assert (change["before"], change["after"]) == ("age >= 75 years", "age >= 80 years")


def test_criteria_without_provenance_still_diff_by_label():
    """A criterion whose source_text is empty must not key on "" and pair with an
    unrelated one that is also missing its provenance."""
    before = _criteria(
        inclusion_quantitative=[_quant(18, ""), _quant(30, "", attribute="egfr", unit="mL")]
    )
    after = _criteria(inclusion_quantitative=[_quant(18, "")])
    (change,) = diff_criteria(before, after)
    assert change["kind"] == "removed"
    assert change["before"] == "egfr >= 30 mL"


def test_missing_and_malformed_buckets_are_tolerated():
    """A checkpoint written by an older version — or a bucket the parser omitted —
    must not make the diff explode; it is an audit aid, not a gate."""
    assert diff_criteria(None, _criteria()) == []
    assert diff_criteria({}, {}) == []
    # A bucket that isn't a list at all (corrupt state) contributes nothing.
    assert diff_criteria({"unparseable": "not a list"}, {"unparseable": "also not"}) == []


def test_edit_record_names_the_editor_and_carries_no_patient_data():
    changes = diff_criteria(
        _criteria(inclusion_quantitative=[_quant(180, "Age 18 or older.")]),
        _criteria(inclusion_quantitative=[_quant(18, "Age 18 or older.")]),
    )
    record = edit_record(3, REVIEWER, changes)
    assert record["revision"] == 3
    assert record["edited_by"] == REVIEWER.email
    assert record["edited_by_role"] == REVIEWER.role
    assert record["edited_at"]
    assert record["changes"] == changes


def test_summary_counts_changes_by_kind():
    changes = diff_criteria(
        _criteria(
            inclusion_quantitative=[_quant(180, "a"), _quant(99, "b")],
            unparseable=["c"],
        ),
        _criteria(
            inclusion_quantitative=[_quant(18, "a")],
            exclusion_categorical=[_cat("x", "c")],
        ),
    )
    # One threshold fixed, one criterion deleted, one unparseable reclassified.
    assert summarize(changes) == "1 modified, 1 reclassified, 1 removed"
    assert summarize([]) == "no changes"
