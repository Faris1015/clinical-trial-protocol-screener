"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ViewModeToggle } from "@/components/view-mode-toggle";
import { useViewMode } from "@/hooks/useViewMode";
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
 * make the finding unauditable, which is the opposite of the point.
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
}: {
  findings: ComplianceFinding[];
  /** The Critic's one-line verdict; shown in plain mode only. */
  summary?: string | null;
  title?: string;
  className?: string;
  region?: string;
}) {
  const { technical } = useViewMode();
  if (!findings.length) return null;

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
          <div key={i} className="flex flex-wrap items-start gap-2 text-sm">
            <Badge variant={finding.severity === "reject" ? "fail" : "warn"}>
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
              <span className="text-muted-foreground shrink-0 font-mono text-xs">
                {finding.rule_id}
              </span>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
