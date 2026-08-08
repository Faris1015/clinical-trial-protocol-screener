"use client";

import { Filter } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCount, formatShare } from "@/lib/metrics";
import { cn } from "@/lib/utils";
import type { CohortAttrition, CriterionAttrition } from "@/types";

/**
 * What is killing the cohort (#94) — every criterion the run applied, ranked by
 * how many patients it screened out.
 *
 * The cohort table beside this answers *who* is eligible. The question a
 * coordinator actually asks is *what* is costing them the panel: "eGFR ≥ 60
 * excludes 41 of 100" is either the protocol working as designed or a threshold
 * worth taking back to the sponsor, and a list of per-patient verdicts cannot say
 * which. This panel is the screen that answers it.
 *
 * Every figure is derived server-side (`backend/app/services/attrition.py`) and
 * arrives on the `/state` payload the page already fetched, so this component
 * renders numbers rather than computing them — and the bucket counts in the
 * caption are `services/cohort.py`'s own, which is what keeps a "5 ineligible"
 * here from contradicting the cohort table under it.
 *
 * The one editorial decision here is that no row shows its exclusion count alone.
 * "Excludes 41" invites the reader to believe relaxing it returns 41 patients when
 * 19 of them also fail something else, so every row that excluded anyone states
 * how much of that is shared and how many patients relaxing it would actually make
 * eligible. A panel that let a coordinator walk away with the wrong delta would be
 * worse than no panel.
 */
export function CohortAttritionPanel({ attrition }: { attrition?: CohortAttrition }) {
  // A run that never reached the Matcher has no cohort to attribute — the page
  // shows nothing rather than an empty table, exactly as the report drops the
  // section. An older payload with no `attrition` key lands here too.
  if (!attrition || attrition.criteria.length === 0) return null;

  const { totals, criteria, overlaps } = attrition;
  const worst = criteria[0];

  return (
    <Card data-region="cohort-attrition">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Filter className="text-muted-foreground size-4" aria-hidden="true" />
          What screened this cohort out
        </CardTitle>
        {/* `eligible` is the denominator every row below is measured against, so
            it belongs in the caption. The full eligible/review/ineligible split is
            *not* repeated here: the cohort table sits directly under this panel and
            already carries it, and the same tally twice on one screen invites a
            reader to check whether the two agree. They come from one server-side
            rule (`services/cohort.py`), so there is nothing to check. */}
        <p className="text-muted-foreground text-xs">
          {formatCount(totals.patients, "patient")} screened, {totals.eligible} eligible ·{" "}
          {formatCount(totals.excluded, "patient")} failed at least one criterion ·{" "}
          {formatCount(totals.unresolved, "patient")} had one that could not be evaluated.
        </p>
        {worst.excluded > 0 && (
          <p className="text-sm">
            <span className="font-medium">{worst.label}</span> is the most restrictive criterion: it
            excludes {formatCount(worst.excluded, "patient")} of {totals.patients}.
          </p>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {criteria.map((row) => (
          <CriterionRow key={row.key} row={row} />
        ))}
        {overlaps.length > 0 && (
          <div className="border-t pt-3" data-region="attrition-overlap">
            <h3 className="text-muted-foreground mb-1.5 text-xs font-medium">
              Exclusions counted twice
            </h3>
            <ul className="space-y-1 text-sm">
              {overlaps.map((overlap) => (
                <li key={`${overlap.a_key}|${overlap.b_key}`} className="text-muted-foreground">
                  <span className="text-foreground">
                    {formatCount(overlap.patients, "patient")}
                  </span>{" "}
                  fail both {overlap.a_label} and {overlap.b_label}.
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * One criterion: its exclusion count, a bar for the share, and the honest delta
 * underneath.
 *
 * The bar is `aria-hidden` — it is a second encoding of the figure beside it, so a
 * screen reader reads the count and the caption and never announces an empty div.
 */
function CriterionRow({ row }: { row: CriterionAttrition }) {
  return (
    <div className="space-y-1" data-region="attrition-criterion" data-criterion={row.key}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="min-w-0 flex-1 text-sm">
          {row.label}{" "}
          <span className="text-muted-foreground text-xs">
            {row.kind === "exclusion" ? "exclusion" : "inclusion"}
          </span>
        </span>
        <span className="text-sm font-medium tabular-nums" aria-hidden="true">
          {row.excluded}
        </span>
        <span className="sr-only">{formatCount(row.excluded, "patient")} excluded</span>
        <span className="text-muted-foreground w-14 text-right text-xs tabular-nums">
          {formatShare(row.share)}
        </span>
      </div>
      <div className="bg-muted h-1.5 overflow-hidden rounded-full" aria-hidden="true">
        {/* Width from the same `share` the figure prints, so the bar can never
            disagree with the number beside it. A row that excluded anyone keeps a
            hairline of colour — a bar that rounds to invisible reads as zero. */}
        <div
          className={cn("bg-status-fail h-full rounded-full", row.excluded > 0 && "min-w-0.5")}
          style={{ width: `${row.share}%` }}
        />
      </div>
      <p className="text-muted-foreground text-xs">{caption(row)}</p>
    </div>
  );
}

/**
 * The line under a row: what relaxing this criterion would actually buy, and what
 * it could not decide.
 *
 * `recoverable` rather than `unique` is what gets called eligible, and the gap
 * between the two is stated when there is one: a patient whose only failure is
 * this criterion but who also has an unresolved one moves into review, not into
 * the cohort. That distinction is the whole reason this panel reports overlap at
 * all.
 */
function caption(row: CriterionAttrition): string {
  const parts: string[] = [];
  if (row.excluded === 0) {
    parts.push(row.unresolved > 0 ? "Excluded nobody" : "Excluded nobody — every patient passed");
  } else if (row.shared === 0) {
    parts.push(
      row.excluded === 1
        ? "Its one exclusion is unique to it"
        : `All ${row.excluded} exclusions are unique to it`
    );
  } else {
    parts.push(`${row.unique} unique, ${row.shared} also failing another criterion`);
  }
  if (row.excluded > 0) {
    parts.push(
      row.recoverable === row.unique
        ? `relaxing it would make ${formatCount(row.recoverable, "patient")} eligible`
        : `relaxing it would make ${formatCount(row.recoverable, "patient")} eligible — the` +
            ` other ${row.unique - row.recoverable} would still need a human`
    );
  }
  if (row.unresolved > 0) {
    parts.push(`could not be evaluated for ${formatCount(row.unresolved, "patient")}`);
  }
  return `${parts.join(" · ")}.`;
}
