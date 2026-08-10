"use client";

import { FileQuestion, Gauge } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCount, formatShare } from "@/lib/metrics";
import { cn } from "@/lib/utils";
import type { Coverage, CoverageGap } from "@/types";

/**
 * How much of this protocol the run could actually check (#93).
 *
 * The criteria table says what was extracted and the cohort table says who
 * matched. Neither answers the question a reviewer has to answer before they
 * trust either: *what fraction of this protocol did we screen on at all*. A
 * protocol where 6 of 20 criteria never parsed produces a cohort that looks
 * exactly like a fully-screened one, and the six sentences nobody checked are the
 * ones a coordinator has to work through by hand.
 *
 * Every figure is derived server-side (`backend/app/services/coverage.py`) from
 * the same checkpoint fields the criteria and criterion statuses are rendered
 * from, so this panel cannot disagree with the tables around it — it renders
 * numbers rather than computing them, down to the percentage.
 *
 * Two editorial decisions. The two layers are always shown apart, because a
 * sentence the vocabulary cannot express and a criterion no patient record can
 * answer are different work; and before a cohort exists the panel says its figure
 * is provisional rather than quietly reporting the parse layer as the whole score.
 * That is the state it is read in at the gate, which is the one place the number
 * can still change a decision.
 */
export function CoveragePanel({ coverage }: { coverage?: Coverage }) {
  // A run with no extraction has no coverage to be a share of — the page shows
  // nothing rather than "0%", exactly as the report drops its section. An older
  // payload with no `coverage` key lands here too.
  if (!coverage || coverage.criteria === 0) return null;

  const { checkable, criteria, score, scored, gaps } = coverage;
  const complete = gaps.length === 0;

  return (
    <Card data-region="coverage" data-score={score}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Gauge className="text-muted-foreground size-4" aria-hidden="true" />
          What we could check
        </CardTitle>
        <p className="text-muted-foreground text-xs">
          {scored
            ? "Criteria this run both structured and evaluated, of every criterion the protocol yielded."
            : "No cohort has been scored yet, so this counts what the extraction structured — criteria the Matcher cannot resolve are not yet reflected."}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5" data-region="coverage-score">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="min-w-0 flex-1 text-sm">
              <span className="font-medium">
                {checkable} of {criteria}
              </span>{" "}
              <span className="text-muted-foreground">
                {criteria === 1 ? "criterion" : "criteria"}
                {scored ? " checkable" : " structured"}
              </span>
            </span>
            <span className="text-sm font-medium tabular-nums">{formatShare(score)}</span>
          </div>
          {/* Width from the same `score` the figure prints, so the bar can never
              disagree with the number beside it. `aria-hidden` because it is a
              second encoding of that figure, not information of its own. */}
          <div className="bg-muted h-1.5 overflow-hidden rounded-full" aria-hidden="true">
            <div
              className={cn(
                "h-full rounded-full",
                checkable > 0 && "min-w-0.5",
                complete ? "bg-status-pass" : "bg-status-warn"
              )}
              style={{ width: `${score}%` }}
            />
          </div>
          <p className="text-muted-foreground text-xs">{caption(coverage)}</p>
        </div>

        {complete ? (
          <p className="text-muted-foreground text-sm" data-region="coverage-complete">
            Every criterion in this extraction was structured
            {scored ? " and evaluated against the cohort" : ""}.
          </p>
        ) : (
          <div className="border-t pt-3" data-region="coverage-gaps">
            <h3 className="text-muted-foreground mb-1.5 flex items-center gap-1.5 text-xs font-medium">
              <FileQuestion className="size-3.5" aria-hidden="true" />
              {/* Neither "criteria" nor "sentences": the list mixes a sentence
                  that never became a criterion with a criterion that could not be
                  evaluated, and naming it by either half would mislabel the other. */}
              What could not be checked ({gaps.length})
            </h3>
            <ul className="space-y-1.5 text-sm">
              {gaps.map((gap, index) => (
                // Keyed by position: the API keeps a repeated `unparseable`
                // sentence, and a criterion the protocol quotes twice is two rows —
                // both by design, so the text is not unique and cannot be the key.
                // The list is server-ordered and read-only, so position is stable.
                <GapRow key={`${gap.reason}|${index}`} gap={gap} />
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * The line under the bar: the two layers apart, so the reader knows which kind of
 * gap they are looking at before reading the list.
 *
 * The match layer is stated only once a cohort exists. Before that `resolved` is 0
 * for want of a Matcher run, not for want of resolvable criteria, and printing
 * "0 of 14 resolved" at the gate would read as a catastrophe rather than as a
 * step that has not happened.
 */
function caption(coverage: Coverage): string {
  const parts: string[] = [];
  if (coverage.unparseable > 0) {
    parts.push(
      `${formatCount(coverage.unparseable, "sentence")} could not be turned into a criterion`
    );
  }
  if (coverage.scored && coverage.unresolved > 0) {
    parts.push(
      `${coverage.unresolved} of ${coverage.structured} structured criteria could not be evaluated` +
        ` against patient records`
    );
  }
  if (parts.length === 0) {
    return coverage.scored
      ? "Every criterion was structured and evaluated."
      : "Every criterion in the protocol was structured.";
  }
  return `${parts.join(" · ")}.`;
}

/**
 * One gap: the reason it went unchecked, the text it went unchecked as, and — for
 * a criterion the Matcher could not settle — how many patients that cost.
 */
function GapRow({ gap }: { gap: CoverageGap }) {
  const unparseable = gap.reason === "unparseable";
  return (
    <li data-region="coverage-gap" data-reason={gap.reason}>
      <span className={cn("text-xs", unparseable ? "text-status-warn" : "text-muted-foreground")}>
        {unparseable ? "Never structured" : "Could not be evaluated"}
        {gap.kind ? ` · ${gap.kind}` : ""}
      </span>
      <p className={unparseable ? "" : "font-medium"}>{gap.text}</p>
      {gap.patients > 0 && (
        <p className="text-muted-foreground text-xs">
          Indeterminate for {formatCount(gap.patients, "patient")}.
        </p>
      )}
    </li>
  );
}
