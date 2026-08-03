"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, ScrollText, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ViewModeToggle } from "@/components/view-mode-toggle";
import { RulesSkeleton } from "@/components/skeletons";
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
 * The compliance rules database, read-only (#57).
 *
 * The reason this page exists is the second half of the issue: a Critic finding
 * names a rule id, and until now that id was unresolvable without opening the
 * repo. Every finding now links here with `?rule=<id>`, so the answer to "why was
 * my protocol blocked" is one click from the block itself — which is what makes
 * the deterministic layer auditable rather than merely deterministic.
 *
 * Fetched from `GET /api/rules` rather than bundled: `RULES_PATH` is deployment
 * configuration, so an instance running amended thresholds must show the ones it
 * is actually enforcing, not the ones that were in the repo at build time.
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
  const [payload, setPayload] = useState<RulesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    let active = true;
    apiFetch("/api/rules")
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          // 503 when the rules file is missing or corrupt — the API's own
          // wording names the path, which is what an operator needs here.
          setError(await problemDetail(response, "Could not load the compliance rules"));
          return;
        }
        setPayload((await response.json()) as RulesPayload);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, []);

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

  // A finding can cite a rule that is no longer on file: the rules file is
  // deployment configuration, and a run checkpointed under an earlier version of
  // it keeps the id it was blocked by forever. Following that link would
  // otherwise land on an ordinary list with nothing highlighted, which reads as
  // "the link is broken" rather than "that rule is gone" — and the second is
  // exactly what an auditor of an old run needs to be told.
  const missingRule = linkedRule && !rules.some((rule) => rule.id === linkedRule);

  return (
    <div className="space-y-4" data-region="rules-index">
      {missingRule && (
        <Card className="border-status-warn/40 bg-status-warn-soft" role="status">
          <CardContent className="flex items-start gap-2.5 text-sm" data-region="rule-not-found">
            <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span className="font-mono">{linkedRule}</span> is not among the rules this instance
              is running. A run screened under an earlier version of the rules file keeps the id it
              was blocked by, even after that rule is changed or removed.
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
      </div>

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
              />
            ))}
          </CardContent>
        </Card>
      )}

      <p className="text-muted-foreground text-sm" aria-live="polite">
        {/* The count is of what is on screen, and the total says what was
            filtered away. The filename is here because an instance can be run
            against an amended rules file (RULES_PATH) — an auditor reading these
            thresholds needs to know which file produced them. */}
        Showing {visible.length} of {rules.length} rules from{" "}
        <span className="font-mono text-xs">{payload?.source}</span>. Read-only: rules are changed
        in the file this instance was deployed with.
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
}: {
  rule: ComplianceRule;
  technical: boolean;
  linked: boolean;
}) {
  return (
    <div
      id={ruleAnchor(rule.id)}
      data-region="rule"
      data-rule={rule.id}
      data-linked={linked || undefined}
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
        <span className="font-mono text-sm font-medium">{rule.id}</span>
        <Badge variant={severityVariant(rule.severity)}>{severityLabel(rule.severity)}</Badge>
        <span className="text-muted-foreground text-xs">{rule.check_label}</span>
        {rule.layer === "semantic" && (
          // The one entry that is not a row of the rules file. Saying so is the
          // honest version of listing it here — a reviewer who followed an
          // LLM-SEM finding must not conclude a model wrote a rule.
          <Badge variant="outline">Model review, not a fixed rule</Badge>
        )}
      </div>

      <p className="text-sm">{technical ? rule.description : rule.plain}</p>

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
    </div>
  );
}
