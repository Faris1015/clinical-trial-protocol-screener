/**
 * Shared vocabulary for the compliance rules viewer (#57) and its editor (#97).
 *
 * The rules page renders these, and every Critic finding links into it, so the
 * URL shape and the severity mapping live here rather than in both places.
 */

import type { CheckKind, ComplianceRule, RuleForm } from "@/types";

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

/**
 * The check kinds an admin may author (#97), in the order the editor offers them.
 *
 * The same four the engine implements and the API accepts. Restated here rather
 * than fetched because the editor needs them before any rule loads, and a fifth
 * kind is a backend release either way — the API rejects an unknown one, so the
 * worst a stale copy can do is fail to *offer* a kind, never accept a bad one.
 */
export const CHECK_KINDS: CheckKind[] = [
  "range",
  "must_be_quantitative",
  "required_attribute",
  "keyword_implies_criterion",
];

const CHECK_KIND_LABELS: Record<CheckKind, string> = {
  range: "Plausible range",
  must_be_quantitative: "Numeric threshold required",
  required_attribute: "Required criterion",
  keyword_implies_criterion: "Implied criterion",
};

/** A check kind's name on screen — the same words `check_label` carries. */
export function checkKindLabel(kind: CheckKind): string {
  return CHECK_KIND_LABELS[kind] ?? kind;
}

/**
 * Whether this check kind tests a *named* attribute.
 *
 * Three of the four do, and read `rule["attribute"]` to find it.
 * `keyword_implies_criterion` is the exception: it asks whether any criterion
 * covers a topic, so it has no single attribute to name — and the server rejects
 * a rule of the other three kinds that omits one. The editor shows the field
 * exactly when the API requires it.
 */
export function needsAttribute(kind: CheckKind): boolean {
  return kind !== "keyword_implies_criterion";
}

/** An empty authoring form, defaulted to the most common check kind. */
export function blankRule(): RuleForm {
  return {
    id: "",
    check: "range",
    attribute: "",
    description: "",
    plain: "",
    keywords: "",
    min_plausible: "",
    max_plausible: "",
    required_category: "",
  };
}

/**
 * An existing rule as the editor's form state.
 *
 * Every field becomes a string — including the two bounds. That is what lets the
 * form distinguish "the author cleared this" from "the author typed 0": a number
 * input bound to `number | undefined` has to represent empty as something, and
 * every candidate (0, NaN, undefined) is either a real bound or a value React
 * warns about. The editor converts back on submit, where empty is sent as null so
 * the API reports the missing bound rather than silently accepting a zero.
 */
export function ruleForm(rule: ComplianceRule): RuleForm {
  return {
    id: rule.id,
    check: rule.check as CheckKind,
    attribute: rule.attribute,
    description: rule.description,
    // The listing falls `plain` back to `description` for display, so a rule with
    // no plain wording would otherwise load into the editor with the two
    // identical — and saving would make that fallback permanent.
    plain: rule.plain === rule.description ? "" : rule.plain,
    keywords: rule.keywords.join(", "),
    min_plausible: rule.min_plausible === undefined ? "" : String(rule.min_plausible),
    max_plausible: rule.max_plausible === undefined ? "" : String(rule.max_plausible),
    required_category: rule.required_category ?? "",
  };
}
