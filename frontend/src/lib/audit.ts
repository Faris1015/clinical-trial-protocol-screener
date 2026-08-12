/**
 * Shared vocabulary for the org-wide decision index (#98).
 *
 * The same arrangement `lib/runs.ts` has for run statuses, and for the same
 * reason: the filter, the table and the empty state all name an action, and three
 * copies of that mapping is how one decision ends up written two ways.
 */

import type { AuditAction } from "@/types";
import { runHref } from "@/lib/runs";

/** Every action, in the order the filter offers them — the API's own order. */
export const AUDIT_ACTIONS: AuditAction[] = [
  "approved",
  "rejected",
  "criteria_revised",
  "escalated",
];

/**
 * The actions worth offering a reader of this role.
 *
 * An escalation is the pipeline's act and carries no person's name, so a reviewer
 * — scoped server-side to their own decisions — can never match one. Offering
 * them the option anyway would answer it with "No decisions match this filter",
 * which reads as *this never happened* rather than *this is not yours to see*:
 * the same ambiguity the 403 on the actor filter exists to avoid. So the option
 * is an admin's.
 */
export function actionsFor(isAdmin: boolean): AuditAction[] {
  return isAdmin ? AUDIT_ACTIONS : AUDIT_ACTIONS.filter((action) => action !== "escalated");
}

const ACTION_LABELS: Record<AuditAction, string> = {
  approved: "Approved",
  rejected: "Rejected",
  criteria_revised: "Criteria revised",
  escalated: "Escalated",
};

/**
 * The label for an action.
 *
 * Entries carry the server's own `label` and the table renders that; this exists
 * for the filter dropdown, which names actions the index may not currently
 * contain and so has nothing to read a label off. Falls back to the raw value for
 * an action a later build adds.
 */
export function actionLabel(action: string): string {
  return ACTION_LABELS[action as AuditAction] ?? action;
}

/**
 * Which badge colour an action reads as, on the clinical status tokens.
 *
 * `approved` is the only one that let a run proceed, so it is the only `pass`.
 * `rejected` stopped a run and reads as `fail` — the same reading the runs index
 * gives the status it produces. An escalation and a revision are both "a human
 * still has to act", which is what `warn` means everywhere else in this app.
 */
export function actionVariant(action: string): "pass" | "fail" | "warn" | "secondary" {
  switch (action) {
    case "approved":
      return "pass";
    case "rejected":
      return "fail";
    case "criteria_revised":
    case "escalated":
      return "warn";
    default:
      return "secondary";
  }
}

/**
 * A `<input type="date">` value as the instant the reader actually meant.
 *
 * Load-bearing, not a nicety. The input emits a bare calendar day, the API reads a
 * bare day as *UTC*, and the table renders every `occurred_at` in the reader's own
 * timezone — so west of UTC a decision stamped `2026-08-01T00:30Z` displays as
 * "Jul 31, 8:30 PM" and a `to=2026-07-31` filter would drop the very row the
 * reader is pointing at. Sending the local day's own bounds as ISO instants (which
 * the API takes and normalizes) makes the filter mean the day the reader sees.
 *
 * `end` widens to the last millisecond of that day, so a range of one day covers
 * all of it — the same rule the API applies to a bare day, in the right zone.
 */
export function dayBound(day: string, end: boolean): string {
  const [year, month, date] = day.split("-").map(Number);
  const at = end
    ? new Date(year, month - 1, date, 23, 59, 59, 999)
    : new Date(year, month - 1, date, 0, 0, 0, 0);
  return at.toISOString();
}

/**
 * Deep link from one entry to what it was about (AC 3).
 *
 * The run, normally. For a criteria revision, the run *anchored at that
 * revision's before/after diff* — the fragment is the id `CriteriaDiff` gives
 * each revision block, so an auditor asking "what did they change?" lands on the
 * answer rather than on a page they then have to scroll.
 */
export function decisionHref(threadId: string, revision: number): string {
  const href = runHref(threadId);
  return revision > 0 ? `${href}#revision-${revision}` : href;
}
