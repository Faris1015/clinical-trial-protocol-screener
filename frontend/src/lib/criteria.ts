/**
 * Shared vocabulary for the review queue and the criteria editor (#53).
 *
 * The option lists mirror the closed enums in `backend/app/schemas/criteria.py`.
 * They are duplicated here on purpose rather than fetched: the app is a static
 * export with no build-time API access, and the backend validates every submitted
 * criterion anyway — so the worst a drifted copy can do is offer an option the
 * API answers 422 to, not corrupt a run. Keeping them in one module means that
 * drift is a one-line fix rather than a hunt through form markup.
 */

import type { CategoricalCriterion, QuantitativeCriterion, ScreeningStatus } from "@/types";

/** `EhrAttribute` — the attributes the Matcher can look up on a patient record. */
export const EHR_ATTRIBUTES = [
  "age",
  "egfr",
  "creatinine",
  "systolic_bp",
  "diastolic_bp",
  "hba1c",
  "bmi",
  "anc",
  "platelets",
  "ecog",
  "ejection_fraction",
] as const;

/** The comparisons a `QuantitativeCriterion` can express. */
export const OPERATORS: QuantitativeCriterion["operator"][] = [
  ">=",
  "<=",
  ">",
  "<",
  "==",
  "between",
];

/** The kinds of thing a `CategoricalCriterion` can be about. */
export const CATEGORIES: CategoricalCriterion["category"][] = [
  "diagnosis",
  "prior_treatment",
  "medication",
  "biomarker",
  "condition",
];

/**
 * Short labels for the buckets a diff entry can name — the editor's own section
 * headings are longer, because there they are the only thing identifying a form
 * section rather than one column of a change line.
 */
const BUCKET_LABELS: Record<string, string> = {
  inclusion_quantitative: "Inclusion · numeric",
  inclusion_categorical: "Inclusion · categorical",
  exclusion_quantitative: "Exclusion · numeric",
  exclusion_categorical: "Exclusion · categorical",
  unparseable: "Unparseable",
};

/** Falls back to the raw key so a bucket the API adds later still renders. */
export function bucketLabel(bucket: string): string {
  return BUCKET_LABELS[bucket] ?? bucket;
}

/**
 * The statuses that put a run in a human's queue.
 *
 * `awaiting_approval` is the gate; `escalated` is the Critic giving up after its
 * retry cap; `failed` is a run that died with an extraction still on file. All
 * three are waiting on a person — which is the whole point of routing the blocked
 * path here and not only to the live screening page's failure banner.
 */
export const REVIEW_STATUSES: ScreeningStatus[] = ["awaiting_approval", "escalated", "failed"];

/**
 * Deep link to one run's criteria editor.
 *
 * A query parameter for the same reason `runHref` uses one: the frontend is a
 * static export, so `/review/<threadId>` would need every id enumerated at build
 * time. See lib/runs.runHref for the full reasoning — including why the trailing
 * slash on the route is load-bearing.
 */
export function reviewHref(threadId: string): string {
  return `/review/edit/?id=${encodeURIComponent(threadId)}`;
}

/** Which badge colour a diff entry reads as. */
export function changeVariant(kind: string): "pass" | "fail" | "warn" | "secondary" {
  switch (kind) {
    case "added":
      return "pass";
    case "removed":
      return "fail";
    case "modified":
    case "reclassified":
      return "warn";
    default:
      return "secondary";
  }
}

/**
 * A blank quantitative criterion carrying `source` as its provenance.
 *
 * Used when reclassifying an `unparseable` sentence: the sentence becomes the new
 * criterion's `source_text`, which is what lets the backend's diff pair the two
 * halves into one "reclassified" entry instead of an unexplained delete plus an
 * unexplained add. `value` starts empty rather than at 0 — a pre-filled zero is a
 * threshold a distracted reviewer can submit without ever having chosen it.
 */
export function blankQuantitative(source: string): QuantitativeCriterion {
  return {
    attribute: "age",
    operator: ">=",
    value: Number.NaN,
    value_high: null,
    unit: "",
    source_text: source,
  };
}

/** A blank categorical criterion carrying `source` as its provenance. */
export function blankCategorical(source: string): CategoricalCriterion {
  return { category: "diagnosis", value: "", negated: false, source_text: source };
}

/**
 * The verbatim sentence a typed criterion would go back to if a reviewer sends it
 * into `unparseable` (#92), or `null` when there is nothing to send back.
 *
 * The round trip is provenance-shaped in both directions: promoting carries the
 * sentence *into* `source_text`, so demoting has to carry that same text back out
 * for the backend's diff to pair the two halves into one "reclassified" entry.
 * A criterion with no recorded sentence has nothing to demote *to* — an empty
 * string in `unparseable` is a blank row a later reviewer can neither act on nor
 * trace — so the editor offers deletion there instead.
 */
export function demotionText(
  criterion: QuantitativeCriterion | CategoricalCriterion
): string | null {
  // Defended like the editor's own provenance blockquote rather than trusted to
  // the type: this reads a checkpoint written by an older revision of the schema,
  // and an absent `source_text` should disable one button, not throw on render.
  const source = criterion.source_text ?? "";
  return source.trim() ? source : null;
}
