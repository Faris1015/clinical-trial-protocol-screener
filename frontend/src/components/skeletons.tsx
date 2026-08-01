import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * Shaped placeholders for the surfaces a screening fills in (#49).
 *
 * Each one is the real component's own box — same card, same header, same
 * columns, same chip height — so the arrival of data is a cross-fade in place
 * rather than the page growing a new section and pushing everything below it
 * down. That is the whole point of shaping them: a generic stack of grey bars
 * would still be "not blank", but it would relayout the moment the parser
 * answers, which on a live pipeline happens under the reviewer's cursor.
 *
 * Widths are deliberately uneven and fixed (not random): a row of identical
 * bars reads as a progress meter, and randomising them would reshuffle on every
 * re-render of a component that re-renders on every streamed frame.
 */

/** Criterion chips are `Badge`-shaped — h-5, fully rounded. */
const CHIP_WIDTHS = ["w-28", "w-20", "w-36", "w-24", "w-32", "w-16", "w-40", "w-24", "w-28"];

/**
 * The live region wrapper every skeleton shares.
 *
 * The bones themselves are `aria-hidden`: a screen reader gets one sentence
 * saying what is being waited on, not a dozen empty divs. `aria-live="polite"`
 * because these mount mid-run, after the page has already been read.
 */
function Placeholder({
  label,
  region,
  children,
}: {
  label: string;
  region: string;
  children: React.ReactNode;
}) {
  return (
    <div role="status" aria-live="polite" data-region={region}>
      <span className="sr-only">{label}</span>
      <div aria-hidden="true">{children}</div>
    </div>
  );
}

/**
 * Stands in for `CriteriaTable` between the upload and the parser's first
 * extraction — the longest blank stretch on the live page, since the router and
 * the parser are both model calls.
 */
export function CriteriaSkeleton() {
  return (
    <Placeholder region="criteria-skeleton" label="Reading the protocol's eligibility criteria…">
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-64 max-w-full" />
        </CardHeader>
        <CardContent className="flex flex-wrap gap-1.5">
          {CHIP_WIDTHS.map((width, i) => (
            <Skeleton key={i} className={cn("h-5 rounded-4xl", width)} />
          ))}
        </CardContent>
      </Card>
    </Placeholder>
  );
}

/** Stands in for `ProtocolView`, whose own pane is the tall half of #54's grid. */
export function ProtocolSkeleton() {
  return (
    <Placeholder region="protocol-skeleton" label="Loading the protocol text…">
      <Card className="gap-0">
        <CardHeader className="gap-2">
          <Skeleton className="h-5 w-36" />
          <Skeleton className="h-3 w-48" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-64 w-full" />
        </CardContent>
      </Card>
    </Placeholder>
  );
}

/**
 * Stands in for `PatientMatchTable` while the matcher scores the cohort — the
 * one wait a reviewer has explicitly asked for by approving the gate, and the
 * longest, since it is one evaluation per patient.
 */
export function CohortSkeleton() {
  return (
    <Placeholder region="cohort-skeleton" label="Matching patients against the approved criteria…">
      <Card>
        <CardHeader className="gap-2">
          <Skeleton className="h-5 w-24" />
          {/* The triage counts a coordinator reads first. */}
          <div className="flex flex-wrap gap-1.5">
            <Skeleton className="h-5 w-24 rounded-4xl" />
            <Skeleton className="h-5 w-28 rounded-4xl" />
            <Skeleton className="h-5 w-24 rounded-4xl" />
          </div>
        </CardHeader>
        <CardContent className="space-y-2">
          {[0, 1, 2, 3, 4].map((row) => (
            // Patient · status · why — the table's three columns, so the header
            // row that lands with the data doesn't shift the rows sideways.
            <div key={row} className="grid grid-cols-[1fr_auto_2fr] items-center gap-2">
              <Skeleton className="h-4 w-40 max-w-full" />
              <Skeleton className="h-5 w-20 rounded-4xl" />
              <Skeleton className="h-4 w-full" />
            </div>
          ))}
        </CardContent>
      </Card>
    </Placeholder>
  );
}

/**
 * The filename / status / thread-id card both rehydrating pages open with.
 */
function RunHeaderSkeleton() {
  return (
    <Card>
      <CardHeader className="gap-2">
        <Skeleton className="h-5 w-72 max-w-full" />
        <Skeleton className="h-3 w-56 max-w-full" />
      </CardHeader>
    </Card>
  );
}

/**
 * The whole run-detail body while a past run rehydrates from its checkpoint
 * (#51). One `GET /state` fills all of it at once, so it is one placeholder:
 * the run header, the four pipeline cards, and the criteria/protocol grid, in
 * the order they will actually appear.
 */
export function RunDetailSkeleton() {
  return (
    <Placeholder region="run-detail-skeleton" label="Loading this screening run…">
      <div className="space-y-4">
        <RunHeaderSkeleton />

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[0, 1, 2, 3].map((card) => (
            <Card key={card} className="gap-0 py-4">
              <CardContent className="space-y-2 px-4">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-5 w-20 rounded-4xl" />
                <Skeleton className="h-3 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>

        <div className="grid items-start gap-3 lg:grid-cols-2">
          <CriteriaSkeleton />
          <ProtocolSkeleton />
        </div>
      </div>
    </Placeholder>
  );
}

/**
 * The edit-and-rerun page (#53) while it rehydrates the run being corrected —
 * the same `GET /state` wait as the replay, opening on the same run header and
 * then the criteria grouped into their editable buckets.
 */
export function CriteriaEditorSkeleton() {
  return (
    <Placeholder region="criteria-editor-skeleton" label="Loading this screening's criteria…">
      <div className="space-y-4">
        <RunHeaderSkeleton />
        <CriteriaSkeleton />
        <CriteriaSkeleton />
      </div>
    </Placeholder>
  );
}
