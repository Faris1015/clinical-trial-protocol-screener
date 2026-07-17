"""Parser golden-set eval (#9): measure LLM extraction quality against a
hand-labeled set — run on demand / nightly, never in the per-PR CI gate (it
needs a real model).

    cd backend && python evals/run_parser_eval.py           # default provider
    LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... python evals/run_parser_eval.py
    LLM_PROVIDER=ollama python evals/run_parser_eval.py      # local Llama

Each `protocols/<name>.txt` is paired with a hand-labeled `labels/<name>.json`
(the expected `CriteriaSchema`). The script runs the *same* extraction path the
Parser node uses, then scores precision/recall per criterion type.

Matching is semantic and *functional*, not string-exact — an LLM phrases
`source_text` and categorical values differently every run, so precision/recall
score what actually changes a screening decision:
  * quantitative — matched on (attribute, operator) with the numeric value(s)
    within a small tolerance; `unit`/`source_text` wording is ignored.
  * categorical  — matched on (negated, value token-overlap Jaccard >= 0.5)
    *within* its inclusion/exclusion bucket. The `category` enum
    (diagnosis/condition/…) is deliberately NOT part of the match key: the
    Matcher never reads it (it searches one combined haystack), so a category
    mislabel does not change who is screened in or out. Category correctness is
    still reported, separately, as a diagnostic.
  * unparseable  — matched on token-overlap (>= 0.4) of the verbatim text.

Exit code is non-zero if any protocol fails to extract (e.g. LLM unavailable),
so a nightly job surfaces a red run instead of a silently empty table.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Make `app` importable no matter the working directory the script is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph.nodes.parser import _extract_criteria  # noqa: E402
from app.schemas.criteria import CriteriaSchema  # noqa: E402
from app.services.llm import get_llm  # noqa: E402

HERE = Path(__file__).resolve().parent
PROTOCOLS = HERE / "protocols"
LABELS = HERE / "labels"
SOURCES = HERE / "sources.json"

# The criterion-type buckets, scored independently.
TYPES = [
    "inclusion_quantitative",
    "inclusion_categorical",
    "exclusion_quantitative",
    "exclusion_categorical",
    "unparseable",
]


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t}


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _quant_match(pred: dict, gold: dict) -> bool:
    if pred["attribute"] != gold["attribute"] or pred["operator"] != gold["operator"]:
        return False
    if abs(float(pred["value"]) - float(gold["value"])) > 0.51:
        return False
    ph, gh = pred.get("value_high"), gold.get("value_high")
    if (ph is None) != (gh is None):
        return False
    return ph is None or abs(float(ph) - float(gh)) <= 0.51


def _cat_match(pred: dict, gold: dict) -> bool:
    # Functional match: `category` is intentionally excluded (the Matcher ignores
    # it). `negated` and the value decide who is screened in/out; the bucket
    # (inclusion vs exclusion) is already handled by scoring the two lists apart.
    return (
        bool(pred["negated"]) == bool(gold["negated"])
        and _jaccard(pred["value"], gold["value"]) >= 0.5
    )


def _text_match(pred: str, gold: str) -> bool:
    return _jaccard(pred, gold) >= 0.4


def _matcher(criterion_type: str):
    if criterion_type == "unparseable":
        return _text_match
    if "quantitative" in criterion_type:
        return _quant_match
    return _cat_match


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: Counts) -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 1.0


def _score(predicted: list, gold: list, match) -> tuple[Counts, list[tuple[dict, dict]]]:
    """Greedy one-to-one matching; returns counts and the matched (pred, gold) pairs."""
    unmatched_gold = list(gold)
    matched: list[tuple[dict, dict]] = []
    for pred in predicted:
        for i, g in enumerate(unmatched_gold):
            if match(pred, g):
                matched.append((pred, g))
                unmatched_gold.pop(i)
                break
    return Counts(tp=len(matched), fp=len(predicted) - len(matched), fn=len(unmatched_gold)), matched


def _origins() -> dict[str, str]:
    """stem -> origin ('curated' | 'clinicaltrials.gov'); default 'curated'."""
    if not SOURCES.exists():
        return {}
    raw = json.loads(SOURCES.read_text())
    return {k: v["origin"] for k, v in raw.items() if isinstance(v, dict) and "origin" in v}


def _cases() -> list[tuple[str, str, dict, str]]:
    origins = _origins()
    cases = []
    for protocol in sorted(PROTOCOLS.glob("*.txt")):
        label = LABELS / f"{protocol.stem}.json"
        if not label.exists():
            raise SystemExit(f"Missing label for {protocol.name}: expected {label}")
        gold = json.loads(label.read_text())
        # Validate the hand label against the live schema so a stale label fails
        # loudly here, not as a mysterious zero score later.
        CriteriaSchema.model_validate(gold)
        origin = origins.get(protocol.stem, "curated")
        cases.append((protocol.stem, protocol.read_text(), gold, origin))
    return cases


def _print_table(title: str, totals: dict[str, Counts]) -> None:
    print(f"\n{title}\n")
    print(f"  {'type':<26} {'precision':>10} {'recall':>8} {'TP':>4} {'FP':>4} {'FN':>4}")
    print(f"  {'-' * 26} {'-' * 10} {'-' * 8} {'-' * 4} {'-' * 4} {'-' * 4}")
    overall = Counts()
    for t in TYPES:
        c = totals[t]
        overall.add(c)
        print(f"  {t:<26} {c.precision:>10.2f} {c.recall:>8.2f} {c.tp:>4} {c.fp:>4} {c.fn:>4}")
    print(f"  {'-' * 26} {'-' * 10} {'-' * 8} {'-' * 4} {'-' * 4} {'-' * 4}")
    print(
        f"  {'OVERALL':<26} {overall.precision:>10.2f} {overall.recall:>8.2f} "
        f"{overall.tp:>4} {overall.fp:>4} {overall.fn:>4}"
    )


def main() -> int:
    cases = _cases()
    if not cases:
        raise SystemExit(f"No protocols found under {PROTOCOLS}")

    structured_llm = get_llm().with_structured_output(CriteriaSchema)
    # Per-origin totals plus a combined bucket, all keyed by criterion type.
    origins_seen = sorted({origin for *_, origin in cases})
    totals = {grp: {t: Counts() for t in TYPES} for grp in [*origins_seen, "all"]}
    cat_correct = 0  # matched categoricals whose `category` enum also agrees
    cat_total = 0
    failures = 0

    print(f"Parser golden-set eval — {len(cases)} protocol(s)\n")
    for stem, text, gold, origin in cases:
        try:
            predicted = _extract_criteria(structured_llm, text).model_dump()
        except Exception as exc:  # noqa: BLE001 — report and keep going
            failures += 1
            print(f"  {stem:<26} [{origin:<17}] EXTRACTION FAILED: {type(exc).__name__}: {exc}")
            continue
        per_case = Counts()
        for t in TYPES:
            c, matched = _score(predicted[t], gold[t], _matcher(t))
            totals[origin][t].add(c)
            totals["all"][t].add(c)
            per_case.add(c)
            if "categorical" in t:
                cat_total += len(matched)
                cat_correct += sum(1 for pred, g in matched if pred["category"] == g["category"])
        print(f"  {stem:<26} [{origin:<17}] P={per_case.precision:.2f}  R={per_case.recall:.2f}")

    # Break out each origin only when the set is genuinely mixed; otherwise the
    # combined table already says everything.
    if len(origins_seen) > 1:
        for origin in origins_seen:
            _print_table(f"{origin} protocols:", totals[origin])
    _print_table("All protocols (combined):", totals["all"])

    # Diagnostic only — not part of P/R. Of the categoricals we matched on value
    # + negated, how often did the model also pick the right `category` enum?
    # The Matcher ignores category, so this never affects screening outcomes.
    if cat_total:
        acc = cat_correct / cat_total
        print(f"\nCategory-label accuracy (diagnostic): {acc:.2f} ({cat_correct}/{cat_total})")

    if failures:
        print(f"\n{failures} protocol(s) failed to extract.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())