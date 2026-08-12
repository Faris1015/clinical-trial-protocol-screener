"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, Archive, Pencil, Plus, RotateCcw, ScrollText, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ViewModeToggle } from "@/components/view-mode-toggle";
import { RulesSkeleton } from "@/components/skeletons";
import { useAuth } from "@/components/AuthProvider";
import { RuleEditor } from "@/components/rules/rule-editor";
import { useViewMode } from "@/hooks/useViewMode";
import { apiFetch, problemDetail } from "@/lib/api";
import { FIELD } from "@/lib/field";
import { matchesQuery, severityLabel, severityVariant } from "@/lib/rules";
import { cn } from "@/lib/utils";
import type { ComplianceRule, RulesPayload } from "@/types";

/** DOM id for one rule's row — what a `?rule=` deep link scrolls to. */
function ruleAnchor(id: string): string {
  return `rule-${id}`;
}

/**
 * The compliance rules database — read for everyone, authored by admins (#57, #97).
 *
 * The reason this page exists is the second half of #57: a Critic finding names a
 * rule id, and until then that id was unresolvable without opening the repo.
 * Every finding links here with `?rule=<id>`, so the answer to "why was my
 * protocol blocked" is one click from the block itself — which is what makes the
 * deterministic layer auditable rather than merely deterministic.
 *
 * #97 adds the write half. **A reviewer's page is unchanged** — same list, same
 * search, same two rationale layers, no controls — and an admin gets an edit
 * affordance on each rule plus a "New rule" button. The gate here is presentation
 * only: every write route is `require_admin`, so hiding the buttons is a courtesy
 * to reviewers rather than the thing that stops them.
 *
 * Fetched from `GET /api/rules` rather than bundled: the rules are a table an
 * admin can change at any moment, so the page must show what the instance is
 * actually enforcing, not what was in the repo at build time.
 *
 * Rendered in the same two layers as the findings (#52) — the toggle picks the
 * rule's plain rationale or its technical one — because a reviewer arriving from
 * a plain-language finding should not hit a wall of regulatory citations.
 */
export function RulesIndex() {
  // `useSearchParams` rather than a route segment: static export, same reasoning
  // as the run detail deep link (see lib/rules.ruleHref).
  const linkedRule = useSearchParams().get("rule");
  const { technical } = useViewMode();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [payload, setPayload] = useState<RulesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  // Which rule's editor is open — its id, or "new" for the create form, or null.
  // One at a time: two open forms would let an admin lose edits by saving the
  // other, and the list is the context each is edited against.
  const [editing, setEditing] = useState<string | null>(null);

  // Bumped after every write to re-run the fetch below. A counter rather than a
  // callable loader because the fetch has to stay *inside* the effect: it owns
  // the `active` guard that drops a response arriving after this component
  // unmounted, and a loader called from an effect body is a synchronous setState
  // in an effect, which is its own bug class.
  const [reloads, setReloads] = useState(0);
  const reload = useCallback(() => setReloads((count) => count + 1), []);

  useEffect(() => {
    let active = true;
    apiFetch("/api/rules")
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          setError(await problemDetail(response, "Could not load the compliance rules"));
          return;
        }
        setPayload((await response.json()) as RulesPayload);
        setError(null);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [reloads]);

  // Re-read the whole listing after a write rather than patching the row in
  // place: the server renders `condition`, `severity` and the attribution stamps,
  // so a locally-assembled row would be this component's guess at what the engine
  // now runs — the exact drift the page exists to rule out.
  const afterSave = useCallback(() => {
    setEditing(null);
    reload();
  }, [reload]);

  const rules = payload?.rules ?? null;
  const visible = useMemo(
    () => (rules ?? []).filter((rule) => matchesQuery(rule, search)),
    [rules, search]
  );

  // Scroll the linked rule into view once — and only once per link. Without the
  // guard, typing in the search box would yank the page back to the linked rule
  // on every keystroke, since the list it sits in re-renders each time.
  const scrolledTo = useRef<string | null>(null);
  useEffect(() => {
    if (!linkedRule || !rules || scrolledTo.current === linkedRule) return;
    const target = document.getElementById(ruleAnchor(linkedRule));
    if (!target) return;
    scrolledTo.current = linkedRule;
    target.scrollIntoView({ block: "center" });
    // Focus as well as scroll: a keyboard or screen-reader user who followed the
    // link is otherwise still at the top of the document, and the ring is the
    // only thing saying which rule they came for.
    target.focus({ preventScroll: true });
  }, [linkedRule, rules]);

  if (error) {
    return (
      <Card className="border-destructive/40 bg-destructive/10" role="alert">
        <CardContent className="flex items-start gap-2.5 text-sm">
          <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </CardContent>
      </Card>
    );
  }

  if (!rules) return <RulesSkeleton />;

  // A finding can cite a rule this instance has never held — a run restored from
  // another deployment, or one screened before the table was seeded. Following
  // that link would otherwise land on an ordinary list with nothing highlighted,
  // which reads as "the link is broken" rather than "that rule is gone".
  //
  // Rarer since #97 than it was: retiring a rule keeps its row, precisely so this
  // notice is not what an auditor of a recent run gets. It stays for the cases
  // retirement does not cover.
  const missingRule = linkedRule && !rules.some((rule) => rule.id === linkedRule);

  return (
    <div className="space-y-4" data-region="rules-index">
      {missingRule && (
        <Card className="border-status-warn/40 bg-status-warn-soft" role="status">
          <CardContent className="flex items-start gap-2.5 text-sm" data-region="rule-not-found">
            <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span className="font-mono">{linkedRule}</span> is not among the rules this instance
              holds. A run keeps the id it was blocked by forever, so a finding can outlive the rule
              that produced it — a retired rule is still listed here, but one this deployment never
              had is not.
            </span>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
            aria-hidden="true"
          />
          <input
            type="search"
            className={`${FIELD} w-full pl-9`}
            placeholder="Search by rule id, attribute or wording"
            aria-label="Search compliance rules"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <ViewModeToggle />
        {isAdmin && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setEditing(editing === "new" ? null : "new")}
            aria-expanded={editing === "new"}
          >
            <Plus aria-hidden="true" />
            New rule
          </Button>
        )}
      </div>

      {editing === "new" && (
        <Card>
          <CardContent className="pt-4">
            <RuleEditor onSaved={afterSave} onCancel={() => setEditing(null)} />
          </CardContent>
        </Card>
      )}

      {visible.length === 0 ? (
        <Card>
          <CardContent
            className="text-muted-foreground flex flex-col items-center gap-2 py-8 text-center text-sm"
            data-region="rules-empty"
          >
            <ScrollText className="size-5" aria-hidden="true" />
            <span>No rule matches “{search}”.</span>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="divide-border divide-y p-0">
            {visible.map((rule) => (
              <RuleRow
                key={rule.id}
                rule={rule}
                technical={technical}
                linked={rule.id === linkedRule}
                isAdmin={isAdmin}
                editing={editing === rule.id}
                onEdit={() => setEditing(editing === rule.id ? null : rule.id)}
                onSaved={afterSave}
                onChanged={reload}
              />
            ))}
          </CardContent>
        </Card>
      )}

      <p className="text-muted-foreground text-sm" aria-live="polite">
        {/* The count is of what is on screen, and the total says what was
            filtered away. `active` is the figure that actually matters to a
            reviewer reading a verdict — retired rules are listed but do not run,
            so "12 rules" alone would overstate what guards their screening. */}
        Showing {visible.length} of {rules.length} rules; {payload?.active ?? 0} are live and
        checked on every screening. Seeded from{" "}
        <span className="font-mono text-xs">{payload?.source}</span>
        {isAdmin
          ? " — since first boot, this list is what the Critic runs, and it is edited here."
          : " — this list is what the Critic runs. Rules are changed by an admin."}
      </p>
    </div>
  );
}

/**
 * One rule: what it tests, how hard it bites, and why it exists.
 *
 * The id is always on screen and always monospaced, in both view modes — it is
 * the join between a finding and this page, and a rules viewer that hid it in
 * plain mode would break the link it exists to serve.
 */
function RuleRow({
  rule,
  technical,
  linked,
  isAdmin,
  editing,
  onEdit,
  onSaved,
  onChanged,
}: {
  rule: ComplianceRule;
  technical: boolean;
  linked: boolean;
  isAdmin: boolean;
  editing: boolean;
  onEdit: () => void;
  onSaved: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retired = !rule.enabled;

  async function toggleEnabled() {
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch(`/api/rules/${encodeURIComponent(rule.id)}/enabled`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: retired }),
      });
      if (!response.ok) {
        setError(await problemDetail(response, "Could not change the rule"));
        return;
      }
      onChanged();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      id={ruleAnchor(rule.id)}
      data-region="rule"
      data-rule={rule.id}
      data-linked={linked || undefined}
      data-retired={retired || undefined}
      // Focusable so the deep link can move the keyboard user here too; -1 keeps
      // it out of the tab order for everyone else, who would otherwise tab
      // through nine non-interactive rows to reach the search box.
      tabIndex={-1}
      className={cn(
        "scroll-mt-4 space-y-2 p-4 outline-none",
        // A ring rather than a background wash: the severity badge is already
        // carrying colour meaning in this row, and a tinted row would read as a
        // second severity.
        linked && "ring-ring rounded-lg ring-2"
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        {/* Dimmed, never hidden: a retired rule is still what a past finding
            cites, so it has to stay findable and readable — it just has to stop
            looking like something that guards the next screening. */}
        <span className={cn("font-mono text-sm font-medium", retired && "text-muted-foreground")}>
          {rule.id}
        </span>
        {retired ? (
          <Badge variant="outline">Retired — not checked</Badge>
        ) : (
          <Badge variant={severityVariant(rule.severity)}>{severityLabel(rule.severity)}</Badge>
        )}
        <span className="text-muted-foreground text-xs">{rule.check_label}</span>
        {rule.layer === "semantic" && (
          // The one entry that is not a row of the rules table. Saying so is the
          // honest version of listing it here — a reviewer who followed an
          // LLM-SEM finding must not conclude a model wrote a rule.
          <Badge variant="outline">Model review, not a fixed rule</Badge>
        )}

        {isAdmin && rule.editable && (
          <span className="ml-auto flex items-center gap-1">
            <Button size="xs" variant="ghost" onClick={onEdit} aria-expanded={editing}>
              <Pencil aria-hidden="true" />
              Edit
            </Button>
            <Button
              size="xs"
              variant={retired ? "ghost" : "destructive"}
              onClick={toggleEnabled}
              disabled={busy}
            >
              {retired ? <RotateCcw aria-hidden="true" /> : <Archive aria-hidden="true" />}
              {retired ? "Restore" : "Retire"}
            </Button>
          </span>
        )}
      </div>

      <p className={cn("text-sm", retired && "text-muted-foreground")}>
        {technical ? rule.description : rule.plain}
      </p>

      <p className="text-muted-foreground font-mono text-xs">{rule.condition}</p>

      {technical && rule.keywords.length > 0 && (
        // Only in the technical layer: the keyword list is how the check decides
        // whether a protocol is even in scope, which is engine mechanics rather
        // than something a coordinator reads a rule for.
        <p className="text-muted-foreground flex flex-wrap items-center gap-1.5 text-xs">
          <span>Applies when the protocol mentions</span>
          {rule.keywords.map((keyword) => (
            <Badge key={keyword} variant="secondary" className="font-mono">
              {keyword}
            </Badge>
          ))}
        </p>
      )}

      {/* Attribution, in the technical layer only and only for admins: it answers
          "who widened this bound", which is an authoring question. The full
          history — including changes to rules since revised again — is the audit
          index (#98), which this deliberately does not duplicate. */}
      {isAdmin && technical && rule.editable && rule.updated_by && (
        <p className="text-muted-foreground text-xs">
          Last changed by <span className="font-medium">{rule.updated_by}</span>
          {rule.updated_at && <> on {new Date(rule.updated_at).toLocaleDateString()}</>}
        </p>
      )}

      {error && (
        <p className="text-destructive text-xs" role="alert">
          {error}
        </p>
      )}

      {editing && (
        <div className="border-border mt-3 border-t pt-3">
          <RuleEditor rule={rule} onSaved={onSaved} onCancel={onEdit} />
        </div>
      )}
    </div>
  );
}
