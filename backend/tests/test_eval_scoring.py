"""Unit tests for the golden-set eval's scoring logic (#9).

The eval harness (`evals/run_parser_eval.py`) is a nightly/on-demand tool that
runs outside the CI gate, but its *scoring* is pure logic that decides every
precision/recall number the README publishes. These tests lock that logic down
so a refactor can't silently change what "matches" — the actual LLM run is not
exercised here (no model, no network), only the matchers and counters.
"""

from evals.run_parser_eval import (
    Counts,
    _cat_match,
    _jaccard,
    _matcher,
    _quant_match,
    _score,
    _text_match,
    _tokens,
)


def _quant(attribute="age", operator=">=", value=18, value_high=None):
    return {"attribute": attribute, "operator": operator, "value": value, "value_high": value_high}


def _cat(value, negated=False, category="condition"):
    return {"category": category, "value": value, "negated": negated}


# --- tokenization / jaccard -----------------------------------------------


def test_tokens_lowercases_and_splits_on_non_alphanumeric():
    assert _tokens("eGFR >= 60 mL/min!") == {"egfr", "60", "ml", "min"}


def test_jaccard_identical_is_one_and_disjoint_is_zero():
    assert _jaccard("heart failure", "heart failure") == 1.0
    assert _jaccard("heart failure", "renal impairment") == 0.0


def test_jaccard_partial_overlap():
    # {type,2,diabetes} vs {type,2,diabetes,mellitus} -> 3/4
    assert _jaccard("type 2 diabetes", "type 2 diabetes mellitus") == 0.75


def test_jaccard_empty_string_is_zero_not_error():
    assert _jaccard("", "anything") == 0.0


# --- quantitative matching -------------------------------------------------


def test_quant_match_same_attribute_operator_and_value():
    assert _quant_match(_quant(), _quant()) is True


def test_quant_match_rejects_different_attribute_or_operator():
    assert _quant_match(_quant(attribute="egfr"), _quant(attribute="age")) is False
    assert _quant_match(_quant(operator="<="), _quant(operator=">=")) is False


def test_quant_match_value_within_and_outside_tolerance():
    assert _quant_match(_quant(value=18.0), _quant(value=18.4)) is True
    assert _quant_match(_quant(value=18), _quant(value=25)) is False


def test_quant_match_between_requires_matching_upper_bound_presence():
    lo_hi = _quant(operator="between", value=40, value_high=80)
    assert _quant_match(lo_hi, _quant(operator="between", value=40, value_high=80)) is True
    # gold has an upper bound, prediction does not -> mismatch
    assert _quant_match(_quant(operator="between", value=40, value_high=None), lo_hi) is False


# --- categorical matching (category is intentionally ignored) --------------


def test_cat_match_ignores_category_enum():
    # Different `category`, same value + negated -> still a match (the Matcher
    # never reads category, so it must not affect scoring).
    assert (
        _cat_match(
            _cat("type 2 diabetes", category="condition"),
            _cat("type 2 diabetes mellitus", category="diagnosis"),
        )
        is True
    )


def test_cat_match_respects_negated_and_value_overlap():
    assert _cat_match(_cat("pregnancy", negated=True), _cat("pregnancy", negated=False)) is False
    assert _cat_match(_cat("acute myeloid leukemia"), _cat("chronic kidney disease")) is False


# --- unparseable (free-text) matching --------------------------------------


def test_text_match_threshold():
    # 2 shared tokens of 4 total -> 0.5 >= 0.4 -> match
    assert _text_match("adequate hepatic function", "adequate hepatic reserve") is True
    assert _text_match("adequate hepatic function", "life expectancy twelve weeks") is False


def test_matcher_dispatches_by_criterion_type():
    assert _matcher("inclusion_quantitative") is _quant_match
    assert _matcher("exclusion_categorical") is _cat_match
    assert _matcher("unparseable") is _text_match


# --- Counts ----------------------------------------------------------------


def test_counts_precision_recall_math():
    c = Counts(tp=3, fp=1, fn=1)
    assert c.precision == 0.75
    assert c.recall == 0.75


def test_counts_empty_is_one_not_zero_division():
    c = Counts()
    assert c.precision == 1.0
    assert c.recall == 1.0


def test_counts_add_accumulates():
    total = Counts()
    total.add(Counts(tp=1, fp=2, fn=3))
    total.add(Counts(tp=4, fp=5, fn=6))
    assert (total.tp, total.fp, total.fn) == (5, 7, 9)


# --- greedy scoring --------------------------------------------------------


def test_score_perfect_match():
    gold = [_quant(attribute="age"), _quant(attribute="egfr", value=60)]
    pred = [_quant(attribute="egfr", value=60), _quant(attribute="age")]  # order-independent
    counts, matched = _score(pred, gold, _quant_match)
    assert (counts.tp, counts.fp, counts.fn) == (2, 0, 0)
    assert len(matched) == 2


def test_score_counts_false_positives_and_negatives():
    gold = [_quant(attribute="age"), _quant(attribute="egfr", value=60)]
    pred = [_quant(attribute="age"), _quant(attribute="bmi", value=25)]
    counts, _matched = _score(pred, gold, _quant_match)
    assert (counts.tp, counts.fp, counts.fn) == (1, 1, 1)


def test_score_is_one_to_one_duplicate_predictions_dont_double_count():
    # Two predictions matching a single gold item: one true positive, one false
    # positive — greedy matching consumes the gold item once.
    gold = [_quant(attribute="age")]
    pred = [_quant(attribute="age"), _quant(attribute="age")]
    counts, matched = _score(pred, gold, _quant_match)
    assert (counts.tp, counts.fp, counts.fn) == (1, 1, 0)
    assert len(matched) == 1


def test_score_returns_matched_pairs_for_diagnostics():
    gold = [_cat("type 2 diabetes", category="diagnosis")]
    pred = [_cat("type 2 diabetes mellitus", category="condition")]
    _counts, matched = _score(pred, gold, _cat_match)
    assert len(matched) == 1
    got_pred, got_gold = matched[0]
    assert got_pred["category"] == "condition"
    assert got_gold["category"] == "diagnosis"
