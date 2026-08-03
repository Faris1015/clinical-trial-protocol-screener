/**
 * Shared vocabulary for the compliance rules viewer (#57).
 *
 * The rules page renders these, and every Critic finding links into it, so the
 * URL shape and the severity mapping live here rather than in both places.
 */

import type { ComplianceRule } from "@/types";

/**
 * Deep link to one rule on the rules page.
 *
 * A query parameter for the same reason `runHref` uses one: the app is a static
 * export, so `/rules/<id>` would need every id enumerated at build time — and
 * the ids come from a YAML file an operator can point elsewhere with `RULES_PATH`.
 * A fragment (`/rules/#BP-001`) would have been the other option, but the page
 * has to *find* the rule as well as scroll to it — a linked rule that a filter is
 * hiding still has to appear — and that is a decision the component makes from a
 * value it can read, not from a hash the browser handles on its own.
 *
 * The trailing slash matters: `trailingSlash: true` exports `rules/index.html`,
 * and without it the client router requests `rules.txt` and hard-reloads.
 */
export function ruleHref(ruleId: string): string {
  return `/rules/?rule=${encodeURIComponent(ruleId)}`;
}

/**
 * What a rule's severity means, in the same words the findings use ("Must fix" /
 * "Advisory"), so a reviewer who followed a finding's link reads the same label
 * on both ends.
 */
export function severityLabel(severity: ComplianceRule["severity"]): string {
  switch (severity) {
    case "reject":
      return "Must fix";
    case "warn":
      return "Advisory";
    case "varies":
      return "Varies";
    default:
      // A check kind the engine has no branch for: the rule is on file but
      // cannot produce a finding, which a reviewer auditing the file should see
      // rather than have hidden behind a blank cell.
      return "Never fires";
  }
}

/** The badge colour for a severity, reusing the clinical status tokens. */
export function severityVariant(
  severity: ComplianceRule["severity"]
): "fail" | "warn" | "secondary" {
  switch (severity) {
    case "reject":
      return "fail";
    case "warn":
      return "warn";
    default:
      return "secondary";
  }
}

/**
 * Whether a rule matches a free-text query.
 *
 * Matched across everything on the row — id, attribute, check kind, threshold,
 * both rationale layers and the keywords: a coordinator searches for "kidney" or
 * "platelets", an engineer for "RENAL-001" or "range", and both should land on
 * the row they meant. Filtering is client-side because the
 * whole database is one small file served in one response — there is nothing to
 * page through, and a round trip per keystroke would be the slower answer.
 */
export function matchesQuery(rule: ComplianceRule, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [rule.id, rule.attribute, rule.check_label, rule.condition, rule.description, rule.plain]
    .concat(rule.keywords)
    .some((field) => field.toLowerCase().includes(needle));
}
