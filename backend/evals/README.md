# Parser golden-set eval

Measures LLM **extraction quality** for the Parser node against a hand-labeled
set. This is a quality gauge for the one non-deterministic component in the
pipeline — it is **not** part of the per-PR CI gate (it needs a real model and
makes network/GPU calls). Run it on demand or on a nightly schedule.

```bash
cd backend
python evals/run_parser_eval.py                                   # default provider (ollama)
LLM_PROVIDER=ollama python evals/run_parser_eval.py               # local Llama via Ollama
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... python evals/run_parser_eval.py
# or, from the repo root:
make eval
```

## Layout

```
evals/
  protocols/<name>.txt     # eligibility section (input)
  labels/<name>.json       # hand-labeled expected CriteriaSchema (ground truth)
  run_parser_eval.py       # runs the Parser's extraction path, scores P/R per type
```

Each protocol is paired to its label by filename stem
(`protocols/01_nsclc_osimertinib.txt` ↔ `labels/01_nsclc_osimertinib.json`).
Labels are validated against the live `CriteriaSchema` at startup, so a stale
label fails loudly rather than scoring zero.

## Golden set

The set mixes two origins (see [`sources.json`](sources.json) for per-protocol
provenance), scored separately and combined:

**Curated** — written for this repo, modeled on real oncology, cardiometabolic,
and renal criteria but not copied from any single record. Kept inside the
`EhrAttribute` vocabulary; they exercise the full attribute set, `between`
ranges, categorical inclusion/exclusion, and a deliberately vague criterion in
`unparseable`. These measure quality on the happy path the pipeline is built for.

| # | Protocol | Therapeutic area | Notable coverage |
|---|----------|------------------|------------------|
| 01 | osimertinib NSCLC | oncology | ecog, anc, platelets; EGFR biomarker; excluded prior TKI |
| 02 | SGLT2i cardio-metabolic | endocrine/cardio | `between` age; hba1c, bmi; excluded low egfr |
| 03 | HFrEF | cardiology | ejection_fraction, systolic_bp; a vague hepatic criterion → `unparseable` |
| 04 | CKD renoprotection | nephrology | `between` egfr; creatinine; excluded diagnoses |
| 05 | stage-2 hypertension | cardiology | systolic_bp, diastolic_bp, bmi; excluded conditions |

**Real** — verbatim eligibility sections from public ClinicalTrials.gov records
(NCT id + URL + access date in `sources.json`). These are deliberately messy —
out-of-vocabulary labs, compound clauses, administrative text — so they measure
robustness on production-shaped input.

| Protocol | NCT | Therapeutic area |
|----------|-----|------------------|
| nct03521154_nsclc | NCT03521154 | oncology (LAURA / osimertinib) |
| nct07433192_t2dm | NCT07433192 | endocrine (luseogliflozin) |
| nct06278844_hfref | NCT06278844 | cardiology (HF pacing) |
| nct06224153_ckd | NCT06224153 | nephrology (stage 4-5 CKD) |

### Labeling convention (real sections)

Real criteria exceed the schema's expressive scope, so ground truth is labeled
under a fixed, documented convention (a judgment call, applied consistently):

- **quantitative** — a numeric threshold on an in-vocabulary attribute (WHO/ECOG
  performance status → `ecog`; "age under 18" as an exclusion → `age < 18`).
- **categorical** — a concrete medical diagnosis / prior treatment / medication
  / biomarker / condition.
- **`unparseable`** — real *medical* criteria the schema cannot represent:
  out-of-vocabulary numerics (e.g. QTc, bilirubin), vague language ("adequate
  organ function"), or compound clauses no single criterion captures (e.g. an
  `LVEF <50% OR LVEF ≥50% and …` disjunction).
- **omitted** — purely administrative / logistical / temporal criteria that are
  not medical eligibility constraints the screener evaluates (informed consent,
  device MRI contraindications, enrollment-timing windows, legal-protection
  clauses).

Because scoring ignores the `category` enum (below), category choices in the
labels never affect P/R.

## Scoring

Matching is **semantic and functional**, not string-exact (an LLM phrases
`source_text` and categorical values differently every run). Precision/recall
score what actually changes a screening decision:

- **quantitative** — matched on `(attribute, operator)` with numeric value(s)
  within a small tolerance; `unit`/`source_text` wording ignored.
- **categorical** — matched on `(negated, value token-overlap ≥ 0.5)` within
  its inclusion/exclusion bucket. The `category` enum
  (`diagnosis`/`condition`/…) is **not** part of the match key: the
  [Matcher](../app/graph/nodes/matcher.py) never reads it (it searches one
  combined haystack of diagnoses + medications + history), so a category
  mislabel screens patients identically. Category correctness is reported
  **separately**, as a diagnostic, not folded into P/R.
- **unparseable** — token-overlap (≥ 0.4) of the verbatim text.

The script prints per-protocol precision/recall, a per-criterion-type aggregate
table, and the category-label accuracy diagnostic. Exit code is non-zero if any
protocol fails to extract, so a nightly job goes red instead of publishing an
empty table.

### Exhaustive vs. subset labels (how to read precision)

Precision and recall are both meaningful **only when the labels are exhaustive**
— every criterion the model could reasonably extract is in the label. That holds
for the **curated** set.

The **real** labels are a curated subset (we omit administrative/compound
criteria by convention), so **precision is confounded**: the model is charged a
false positive for every real criterion we chose not to label — most visibly the
~20 administrative items it dumps into `unparseable`. On the real set, **read
recall** ("of the criteria we said matter, how many did the model find?") and
treat precision as a pessimistic floor, not a fair metric. The harness prints
both (it is a neutral measurement tool); the interpretation lives here and in the
top-level README.

## Adding a case

1. Drop the eligibility section in `protocols/<name>.txt`.
2. Hand-label the expected extraction in `labels/<name>.json` (must validate
   against `CriteriaSchema`; only attributes in the `EhrAttribute` vocabulary
   are checkable downstream).
3. Re-run — the new pair is picked up automatically.