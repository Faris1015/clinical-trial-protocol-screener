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
 *
 * `plural` is for the nouns an "s" does not pluralize — "criterion" is the one the
 * coverage score (#93) counts in, and it appears in a screen-reader label as well
 * as in prose, so getting it right is not cosmetic.
 */
export function formatCount(count: number, noun: string, plural = `${noun}s`): string {
  return `${count} ${count === 1 ? noun : plural}`;
}

/**
 * A dollar figure at a precision that shows it (#101).
 *
 * A screening costs cents, so a fixed two decimals would print every real figure
 * as "$0.00" — the one rendering that turns a working cost accountant into a
 * broken-looking one. Below a cent the value keeps four decimals; at or above a
 * cent it takes the conventional two. `null` is not a zero: it is the API saying
 * it has nothing to estimate from, and reads as an em dash rather than as free.
 */
export function formatUsd(cost: number | null | undefined): string {
  if (cost === null || cost === undefined) return "—";
  if (cost > 0 && cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

/**
 * A token count, thousands-separated. Token counts run to five and six figures on
 * a real protocol, where an unseparated run of digits is unreadable at a glance.
 */
export function formatTokens(tokens: number): string {
  return tokens.toLocaleString("en-US");
}

/**
 * A duration in the unit a reader can hold: milliseconds under a second, seconds
 * above it. Node durations span three orders of magnitude — a deterministic
 * router in single-digit milliseconds, a cohort mapping call in tens of seconds —
 * and one fixed unit makes one end of that range unreadable.
 *
 * `null` is the API declining to estimate a percentile (see
 * `MetricsSummary.estimated_percentiles`), not a zero.
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
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
