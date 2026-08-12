/**
 * Shared vocabulary for the org-wide decision index (#98).
 *
 * The same arrangement `lib/runs.ts` has for run statuses, and for the same
 * reason: the filter, the table and the empty state all name an action, and three
 * copies of that mapping is how one decision ends up written two ways.
 */

import type { AuditAction, AuditEntry } from "@/types";
import { runHref } from "@/lib/runs";
import { ruleHref } from "@/lib/rules";

/** Every action, in the order the filter offers them — the API's own order. */
export const AUDIT_ACTIONS: AuditAction[] = [
  "approved",
  "rejected",
  "criteria_revised",
  "escalated",
  "rule_created",
  "rule_updated",
  "rule_disabled",
  "rule_enabled",
];

/**
 * The actions a reviewer can ever match, and so the only ones worth offering them.
 *
 * An escalation is the pipeline's act and carries no person's name, so a reviewer
 * — scoped server-side to their own decisions — can never match one. Offering
 * them the option anyway would answer it with "No decisions match this filter",
 * which reads as *this never happened* rather than *this is not yours to see*:
 * the same ambiguity the 403 on the actor filter exists to avoid. So the option
 * is an admin's.
 *
 * The rule actions (#97) are the same case for the same reason: authoring is
 * `require_admin`, so a reviewer's own decisions can never include one.
 */
const REVIEWER_UNREACHABLE: AuditAction[] = [
  "escalated",
  "rule_created",
  "rule_updated",
  "rule_disabled",
  "rule_enabled",
];

export function actionsFor(isAdmin: boolean): AuditAction[] {
  return isAdmin
    ? AUDIT_ACTIONS
    : AUDIT_ACTIONS.filter((action) => !REVIEWER_UNREACHABLE.includes(action));
}

const ACTION_LABELS: Record<AuditAction, string> = {
  approved: "Approved",
  rejected: "Rejected",
  criteria_revised: "Criteria revised",
  escalated: "Escalated",
  rule_created: "Rule created",
  rule_updated: "Rule updated",
  rule_disabled: "Rule retired",
  rule_enabled: "Rule restored",
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
 *
 * Retiring a rule reads as `fail` for the same reason a rejection does: it is the
 * one rule action that leaves a guardrail *not* running, and an auditor scanning
 * a page of entries should find it without reading every label. The other three
 * are neutral — authoring and revising rules is ordinary admin work.
 */
export function actionVariant(action: string): "pass" | "fail" | "warn" | "secondary" {
  switch (action) {
    case "approved":
      return "pass";
    case "rejected":
    case "rule_disabled":
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
 * Deep link from one entry to what it was about (#98 AC 3, #97).
 *
 * The run, normally. For a criteria revision, the run *anchored at that
 * revision's before/after diff* — the fragment is the id `CriteriaDiff` gives
 * each revision block, so an auditor asking "what did they change?" lands on the
 * answer rather than on a page they then have to scroll.
 *
 * For a rule mutation there is no run: the entry points at the rule on the rules
 * page, which is where its current wording and its retired/live state are. The
 * branch is on `subject_kind` rather than on the action name so the list of rule
 * actions lives in one place — the server's — instead of being restated here.
 */
export function decisionHref(
  entry: Pick<AuditEntry, "subject_kind" | "subject_id" | "thread_id" | "revision">
): string {
  if (entry.subject_kind === "rule") return ruleHref(entry.subject_id);
  const href = runHref(entry.thread_id);
  return entry.revision > 0 ? `${href}#revision-${entry.revision}` : href;
}

/**
 * What an entry points at, in words — the label on its deep link.
 *
 * A thread_id is a UUID and reads as one; a rule id reads as itself. Both are
 * rendered monospaced by the table, so this only has to answer *which* id.
 */
export function decisionSubject(
  entry: Pick<AuditEntry, "subject_kind" | "subject_id" | "thread_id">
): string {
  return entry.subject_kind === "rule" ? entry.subject_id : entry.thread_id;
}
