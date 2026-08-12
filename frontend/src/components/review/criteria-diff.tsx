import { ArrowRight, PencilLine } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { bucketLabel, changeVariant } from "@/lib/criteria";
import { formatTimestamp } from "@/lib/runs";
import type { CriteriaEdit } from "@/types";

/**
 * The before/after diff of every reviewer revision of a run's criteria (#53).
 *
 * Read back from `criteria_edits` in the graph state, so it renders the same on
 * the editor immediately after a re-run and on a past run's detail page months
 * later — the record is the checkpoint's, not this component's. Newest revision
 * first: after a re-run, the change a reviewer just made is the one they are
 * looking for.
 *
 * Renders nothing for a run nobody has edited, which is most of them.
 */
export function CriteriaDiff({ edits }: { edits: CriteriaEdit[] }) {
  if (edits.length === 0) return null;
  const revisions = [...edits].sort((a, b) => b.revision - a.revision);

  return (
    <Card data-region="criteria-diff">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <PencilLine className="text-status-warn size-4" aria-hidden="true" />
          Reviewer edits
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {revisions.map((edit) => (
          <div
            key={edit.revision}
            // The link target the audit index (#98) points a criteria-revision
            // entry at: an auditor asking "what did they change?" lands on that
            // revision's diff rather than at the top of a long replay. `scroll-mt`
            // keeps the heading clear of the sticky top bar when it does.
            id={`revision-${edit.revision}`}
            className="scroll-mt-20 space-y-2"
            data-revision={edit.revision}
          >
            <p className="text-muted-foreground text-xs">
              Revision {edit.revision} · {edit.edited_by}
              {edit.edited_by_role ? ` (${edit.edited_by_role})` : ""}
              {edit.edited_at ? ` · ${formatTimestamp(edit.edited_at)}` : ""}
            </p>
            {edit.changes.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                Re-run with no changes to the criteria.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {edit.changes.map((change, i) => (
                  <li
                    key={i}
                    className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm"
                    data-kind={change.kind}
                  >
                    <Badge variant={changeVariant(change.kind)}>{change.kind}</Badge>
                    <span className="text-muted-foreground text-xs">
                      {change.from_bucket
                        ? `${bucketLabel(change.from_bucket)} → ${bucketLabel(change.bucket)}`
                        : bucketLabel(change.bucket)}
                    </span>
                    {/* Struck-through rather than only colour-coded: the
                        distinction between the old and the new value has to
                        survive a monochrome print-out of an audit trail. */}
                    {change.before && (
                      <span className="text-muted-foreground font-mono text-xs line-through">
                        {change.before}
                      </span>
                    )}
                    {change.before && change.after && (
                      <ArrowRight
                        className="text-muted-foreground size-3 shrink-0"
                        aria-label="changed to"
                      />
                    )}
                    {change.after && <span className="font-mono text-xs">{change.after}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
