/**
 * The cohort's triage vocabulary — which bucket a patient is in, and how it reads.
 *
 * Mirrors `backend/app/services/cohort.py`, which is where the rule is enforced for
 * the runs index's match count, the exported report and the run comparison (#59).
 * This copy exists because the live screening page and the cohort table bucket
 * evaluations client-side, straight out of the streamed frames — there is no
 * request to ask. Keeping it in one module means the two views that do it (the
 * cohort table and anything comparing verdicts) can't drift from each other.
 *
 * `needs_review` outranks `eligible`: a patient the Matcher could not fully
 * determine has to reach a human even if every criterion it *could* evaluate
 * passed, so counting them as eligible would hand a coordinator an unreviewed
 * candidate.
 */

import type { PatientEvaluation } from "@/types";

export type CohortBucket = "eligible" | "ineligible" | "review";

export function bucketOf(evaluation: PatientEvaluation): CohortBucket {
  if (evaluation.needs_review) return "review";
  return evaluation.eligible ? "eligible" : "ineligible";
}

/** Which badge colour a bucket reads as — the clinical status tokens. */
export function cohortVariant(bucket: string): "pass" | "fail" | "warn" | "secondary" {
  switch (bucket) {
    case "eligible":
      return "pass";
    case "ineligible":
      return "fail";
    case "review":
      return "warn";
    default:
      // A bucket the API adds later still renders, just without a claim about
      // whether it is good news.
      return "secondary";
  }
}
