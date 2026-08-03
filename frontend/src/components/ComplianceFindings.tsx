"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ViewModeToggle } from "@/components/view-mode-toggle";
import { useViewMode } from "@/hooks/useViewMode";
import { ruleHref } from "@/lib/rules";
import type { ComplianceFinding } from "@/types";

/** Plain-language names for what a severity means to the person reading it. */
const SEVERITY_LABEL = { reject: "Must fix", warn: "Advisory" } as const;

/**
 * The Critic's findings, in whichever layer the reader asked for (#52).
 *
 * Plain mode leads with the reviewer-facing explanation and says what the
 * severity *means* ("Must fix" / "Advisory") instead of naming the rule that
 * fired; technical mode is the rule id and the engine's own wording. The rule id
 * is on screen either way — a plain-language view that dropped provenance would
 * make the finding unauditable, which is the opposite of the point — and in both
 * modes it links to that rule on the rules page (#57), so the id resolves to what
 * it actually checks instead of being a string only the repo can explain.
 *
 * Shared by the run replay and the criteria editor: both show findings, so the
 * two layers are rendered in one place rather than drifting apart.
 */
export function ComplianceFindings({
  findings,
  summary,
  title = "Compliance findings",
  className,
  region = "compliance-findings",
  ruleLinksInNewTab = false,
}: {
  findings: ComplianceFinding[];
  /** The Critic's one-line verdict; shown in plain mode only. */
  summary?: string | null;
  title?: string;
  className?: string;
  region?: string;
  /**
   * Open the rule links in a new tab. For the criteria editor, whose page holds
   * an unsaved draft: looking up what a rule checks must not be a way to lose
   * the corrections you were making to satisfy it. The read-only replay leaves
   * this false — there is nothing to lose there, and hijacking the back button
   * on a reference lookup is its own annoyance.
   */
  ruleLinksInNewTab?: boolean;
}) {
  const { technical } = useViewMode();
  if (!findings.length) return null;

  // `noopener` is the half that matters even in the same origin: without it the
  // rules tab gets a live `window.opener` handle back to the editor.
  const linkTarget = ruleLinksInNewTab
    ? ({ target: "_blank", rel: "noopener noreferrer" } as const)
    : {};

  // The Critic's summary leads with the first finding's own explanation, so with
  // a single finding it is the row below it, word for word. Show it only when it
  // adds something the rows don't: the count across several findings.
  const showSummary = !technical && summary && findings.length > 1;

  return (
    <Card data-region={region} className={className}>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">{title}</CardTitle>
          <ViewModeToggle />
        </div>
        {showSummary && <p className="text-sm">{summary}</p>}
      </CardHeader>
      <CardContent className="space-y-2">
        {findings.map((finding, i) => (
          <div key={i} className="flex flex-wrap items-start gap-2 text-sm" data-region="finding">
            {/* Whichever element carries the rule id is the link to the rule that
                fired (#57) — the badge in technical mode, the trailing id in
                plain. Rendering the badge *as* the anchor rather than wrapping it
                keeps one focusable element per finding, and keeps the hit target
                the chip a reader is already aiming at. */}
            <Badge
              variant={finding.severity === "reject" ? "fail" : "warn"}
              render={
                technical ? <Link href={ruleHref(finding.rule_id)} {...linkTarget} /> : undefined
              }
              title={technical ? `What ${finding.rule_id} checks` : undefined}
              // The status variants carry no `[a]:hover` rule of their own — a
              // severity chip must not change colour on hover — so the affordance
              // is an underline, applied only when the badge is a link.
              className={technical ? "underline-offset-2 hover:underline" : undefined}
            >
              {technical ? finding.rule_id : SEVERITY_LABEL[finding.severity]}
            </Badge>
            <span className="min-w-0 flex-1">
              {/* A run screened before #52 has no explanation in its checkpoint;
                  its technical message is a worse read than a written one, but it
                  is the truth about that run and beats an empty line. */}
              {/* `||`, not `??`: an explanation that came back empty is as
                  useless as a missing one, and both fall back the same way. */}
              {technical ? finding.message : finding.explanation || finding.message}
            </span>
            {!technical && (
              <Link
                href={ruleHref(finding.rule_id)}
                title={`What ${finding.rule_id} checks`}
                className="text-muted-foreground hover:text-foreground shrink-0 font-mono text-xs underline-offset-4 hover:underline"
                {...linkTarget}
              >
                {finding.rule_id}
              </Link>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
