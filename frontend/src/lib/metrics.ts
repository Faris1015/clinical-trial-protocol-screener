/**
 * Shared vocabulary for the metrics summary (#58).
 *
 * The page renders three panels of the same shape — a labelled count, its share,
 * and a bar — so the formatting and the colour mapping live here rather than
 * being repeated (and drifting) three times. `formatCount` outgrew that page and
 * is now the app's one count-with-noun helper (the cohort attrition panel, #94,
 * reads it too): "2 patients" and "1 run" must pluralize by one rule.
 */

import type { FunnelOutcome } from "@/types";

/**
 * A share as it appears on screen. The API rounds to one decimal, so this only
 * adds the sign: `75` prints as "75%", `16.7` as "16.7%".
 */
export function formatShare(share: number): string {
  return `${share}%`;
}

/**
 * A quantity with its noun, pluralized — for the figures a reviewer reads as a
 * sentence ("2 escalated runs", "1 attempt on average") rather than as a table
 * cell. Fractions take the plural: "2.33 attempts", and only an exact 1 is
 * singular.
 *
 * The API has already rounded every figure it sends, and number→string drops the
 * zeros that rounding leaves, so a mean of exactly one reads "1 attempt" rather
 * than "1.00 attempts". Rounding stays the API's job.
 */
export function formatCount(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * Which bar colour a funnel outcome reads as, reusing the clinical status tokens
 * the run badges use — `done` is the only success, `failed` the only outright
 * failure, and an escalation is neither (it is the gate working, and a human is
 * expected).
 *
 * Only the funnel is colour-coded. The rejection and attempt panels are
 * distributions, where every row is a neutral fact about how the pipeline
 * behaves; painting the tail of a histogram red would read as an alarm about a
 * Critic loop doing exactly what it exists to do.
 */
export function outcomeBarClass(outcome: FunnelOutcome["outcome"]): string {
  switch (outcome) {
    case "done":
      return "bg-status-pass";
    case "failed":
      return "bg-status-fail";
    case "escalated":
      return "bg-status-warn";
    default:
      return "bg-muted-foreground";
  }
}
