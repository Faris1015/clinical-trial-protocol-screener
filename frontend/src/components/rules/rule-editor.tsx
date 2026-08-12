"use client";

import { useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, problemDetail } from "@/lib/api";
import { CHECK_KINDS, blankRule, checkKindLabel, needsAttribute, ruleForm } from "@/lib/rules";
import { FIELD } from "@/lib/field";
import type { ComplianceRule, RuleForm } from "@/types";

/**
 * The admin's authoring form for one deterministic rule (#97, AC 6).
 *
 * Reviewers never render this — `RulesIndex` gates it on the role — but that is a
 * convenience, not the control: `POST`/`PATCH /api/rules` are `require_admin`, so
 * a reviewer who reached this form anyway would get a 403 from the server. The UI
 * hides what the API refuses; it does not decide it.
 *
 * **Which fields appear depends on the check kind**, and it changes as the author
 * picks one. That mirrors the server's validation exactly (`services/rules.validate`):
 * a `range` needs two bounds and nothing else, a `keyword_implies_criterion` needs
 * keywords and a category and no attribute. Showing every field for every kind
 * would invite an author to fill in bounds the engine will never read, and then
 * wonder why their rule does nothing.
 *
 * **The server is still the validator.** Nothing here re-implements the rules —
 * the form makes the right shape easy to type, and a 422 from the API is rendered
 * verbatim, because the API's message names the specific problem and a second
 * copy of those checks in TypeScript is a second thing to keep in sync.
 */
export function RuleEditor({
  rule,
  onSaved,
  onCancel,
}: {
  /** The rule being revised, or undefined to author a new one. */
  rule?: ComplianceRule;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<RuleForm>(rule ? ruleForm(rule) : blankRule());
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const editing = rule !== undefined;

  function set<K extends keyof RuleForm>(key: K, value: RuleForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    // Only the fields this check kind uses are sent. The API forbids unknown
    // fields, and a `range` rule carrying a `required_category` would be one.
    const body: Record<string, unknown> = {
      id: form.id.trim(),
      check: form.check,
      description: form.description.trim(),
      attribute: needsAttribute(form.check) ? form.attribute.trim() : "",
      plain: form.plain.trim(),
      keywords: form.keywords
        .split(",")
        .map((word) => word.trim())
        .filter(Boolean),
    };
    if (form.check === "range") {
      // Empty stays empty rather than becoming 0: `Number("")` is 0, and a bound
      // the author never typed must reach the API as missing so it says so,
      // rather than as a silent zero that validates.
      body.min_plausible = form.min_plausible === "" ? null : Number(form.min_plausible);
      body.max_plausible = form.max_plausible === "" ? null : Number(form.max_plausible);
    }
    if (form.check === "keyword_implies_criterion") {
      body.required_category = form.required_category.trim();
    }

    try {
      const response = await apiFetch(
        editing ? `/api/rules/${encodeURIComponent(rule.id)}` : "/api/rules",
        {
          method: editing ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      if (!response.ok) {
        setError(await problemDetail(response, "Could not save the rule"));
        return;
      }
      onSaved();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="space-y-3" onSubmit={save} data-region="rule-editor">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className="text-sm font-medium">Rule id</span>
          <input
            className={`${FIELD} w-full font-mono disabled:opacity-60`}
            value={form.id}
            onChange={(e) => set("id", e.target.value)}
            // A rule id is what every past finding cites, so an edit can never
            // change it — the server ignores the field on PATCH, and showing it
            // editable here would promise something the API would not honour.
            disabled={editing}
            placeholder="RENAL-002"
            required
          />
        </label>

        <label className="space-y-1">
          <span className="text-sm font-medium">Check kind</span>
          <select
            className={`${FIELD} w-full`}
            value={form.check}
            onChange={(e) => set("check", e.target.value as RuleForm["check"])}
          >
            {CHECK_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {checkKindLabel(kind)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {needsAttribute(form.check) && (
        <label className="space-y-1 block">
          <span className="text-sm font-medium">Attribute</span>
          <input
            className={`${FIELD} w-full font-mono`}
            value={form.attribute}
            onChange={(e) => set("attribute", e.target.value)}
            placeholder="egfr"
            required
          />
        </label>
      )}

      {form.check === "range" && (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1">
            <span className="text-sm font-medium">Minimum plausible</span>
            <input
              type="number"
              step="any"
              className={`${FIELD} w-full`}
              value={form.min_plausible}
              onChange={(e) => set("min_plausible", e.target.value)}
              required
            />
          </label>
          <label className="space-y-1">
            <span className="text-sm font-medium">Maximum plausible</span>
            <input
              type="number"
              step="any"
              className={`${FIELD} w-full`}
              value={form.max_plausible}
              onChange={(e) => set("max_plausible", e.target.value)}
              required
            />
          </label>
        </div>
      )}

      {form.check === "keyword_implies_criterion" && (
        <label className="space-y-1 block">
          <span className="text-sm font-medium">Required category</span>
          <input
            className={`${FIELD} w-full font-mono`}
            value={form.required_category}
            onChange={(e) => set("required_category", e.target.value)}
            placeholder="condition"
            required
          />
        </label>
      )}

      <label className="space-y-1 block">
        <span className="text-sm font-medium">
          Keywords
          <span className="text-muted-foreground ml-1.5 font-normal">
            comma separated
            {form.check === "range" || form.check === "required_attribute"
              ? " — optional for this check kind"
              : " — required: they are what bring the rule into play"}
          </span>
        </span>
        <input
          className={`${FIELD} w-full font-mono`}
          value={form.keywords}
          onChange={(e) => set("keywords", e.target.value)}
          placeholder="renal, kidney, egfr"
        />
      </label>

      <label className="space-y-1 block">
        <span className="text-sm font-medium">
          Description
          <span className="text-muted-foreground ml-1.5 font-normal">
            technical — this wording is fed back to the Parser
          </span>
        </span>
        <textarea
          className={`${FIELD} h-auto w-full py-2`}
          rows={2}
          value={form.description}
          onChange={(e) => set("description", e.target.value)}
          required
        />
      </label>

      <label className="space-y-1 block">
        <span className="text-sm font-medium">
          Plain wording
          <span className="text-muted-foreground ml-1.5 font-normal">
            optional — falls back to the description
          </span>
        </span>
        <textarea
          className={`${FIELD} h-auto w-full py-2`}
          rows={2}
          value={form.plain}
          onChange={(e) => set("plain", e.target.value)}
        />
      </label>

      {error && (
        <p
          className="text-destructive flex items-start gap-2 text-sm"
          role="alert"
          data-region="rule-editor-error"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}

      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={saving}>
          {saving && <Loader2 className="animate-spin" aria-hidden="true" />}
          {editing ? "Save changes" : "Create rule"}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <span className="text-muted-foreground text-xs">
          Takes effect on the next screening; runs already in flight keep the rules they started
          with.
        </span>
      </div>
    </form>
  );
}
