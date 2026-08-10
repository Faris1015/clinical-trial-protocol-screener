"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  GitCompare,
  History,
  Search,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiFetch, problemDetail } from "@/lib/api";
import { FIELD } from "@/lib/field";
import { formatShare } from "@/lib/metrics";
import {
  RUN_STATUSES,
  compareHref,
  formatTimestamp,
  runHref,
  statusLabel,
  statusVariant,
} from "@/lib/runs";
import { cn } from "@/lib/utils";
import type { CoverageSummary, Screening, ScreeningPage, ScreeningStatus } from "@/types";

const PAGE_SIZE = 25;

/** A comparison is between exactly two runs (#59), so the selection stops there. */
const COMPARE_LIMIT = 2;

/**
 * A run held for comparison. The filename travels with the id: a coordinator can
 * pick one run, page on, and pick the second, and the action bar still has to name
 * what it is about to compare after the row itself has scrolled out of the result
 * set.
 */
type Held = { threadId: string; filename: string };

// Typing shouldn't fire a request per keystroke — the endpoint is rate limited
// (RATE_LIMIT_READ) and a half-typed protocol name isn't a query anyone wants
// answered.
const SEARCH_DEBOUNCE_MS = 300;

/**
 * The runs index (#51): every screening this instance has processed, newest
 * first, with the controls needed to find one among hundreds.
 *
 * Filtering and paging happen server-side (`GET /api/screenings` takes
 * `limit`/`offset`/`status`/`q`), not over a client-side copy of the table:
 * the list grows without bound, and the row a coordinator is looking for is
 * usually not on the page they'd be handed.
 */
export function RunsIndex() {
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [status, setStatus] = useState<ScreeningStatus | "">("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ScreeningPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The runs held for a side-by-side comparison (#59), in the order they were
  // picked: the first is the comparison's left column, and the diff's additions and
  // removals are stated from its point of view. Deliberately not reset by a filter
  // change or a page turn — picking the two runs to compare is often exactly why
  // someone searches twice.
  const [held, setHeld] = useState<Held[]>([]);

  // The query string whose response is currently on screen. `loading` is
  // *derived* from it rather than stored, which keeps every setState in this
  // component inside a promise callback — the effect below never has to flip a
  // flag synchronously on its way to the fetch.
  const [settledQuery, setSettledQuery] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [search]);

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (status) params.set("status", status);
    if (debouncedSearch) params.set("q", debouncedSearch);
    return params.toString();
  }, [offset, status, debouncedSearch]);

  // The offset of the page currently on screen. A ref as well as being derived
  // below, because the fetch's failure path has to read it without making this
  // effect depend on (and so re-run for) the loaded page.
  const shownOffsetRef = useRef(0);

  useEffect(() => {
    // `active` is the out-of-order guard: React runs this cleanup before
    // re-running the effect, so a slow response for "nsc" can no longer land
    // after the fast one for "nsclc" and overwrite it with stale rows.
    let active = true;

    const failed = (detail: string) => {
      // Leave the previous page on screen under the error rather than blanking
      // the table — a transient 429 shouldn't look like "no runs". But put the
      // requested offset back to the page still displayed: without that, a
      // failed "Next" would leave offset one page ahead of the visible rows, and
      // the *next* click would skip straight over the page that failed. If this
      // does move the offset, the effect re-runs and re-requests the visible
      // page, which is the retry the user would otherwise have to trigger.
      setError(detail);
      setOffset(shownOffsetRef.current);
      setSettledQuery(query);
    };

    apiFetch(`/api/screenings?${query}`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          const detail = await problemDetail(response, "Could not load past runs");
          if (!active) return;
          failed(detail);
          return;
        }
        const body = (await response.json()) as ScreeningPage;
        if (!active) return;
        setError(null);
        setPage(body);
        shownOffsetRef.current = body.offset;
        setSettledQuery(query);
      })
      .catch(() => {
        if (active) failed("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [query]);

  // Changing a filter resets paging here, in the handlers, rather than in an
  // effect watching the filters: staying on offset 50 of a narrower result set
  // shows an empty page that reads as "no runs match". The ref is reset too, or
  // a failed first page of the new filter would roll the offset back to a page
  // that belonged to the old one.
  function resetPaging() {
    setOffset(0);
    shownOffsetRef.current = 0;
  }

  function changeSearch(value: string) {
    setSearch(value);
    resetPaging();
  }

  function changeStatus(value: ScreeningStatus | "") {
    setStatus(value);
    resetPaging();
  }

  /**
   * Hold or release one run.
   *
   * At the limit, a further checkbox is disabled rather than silently evicting the
   * older selection: which two runs get compared — and in which order — is the
   * whole input to the next page, and having a third click quietly reassign it is
   * how someone ends up reading a diff of the wrong pair.
   */
  function toggleHeld(run: Screening) {
    setHeld((current) => {
      const without = current.filter((entry) => entry.threadId !== run.thread_id);
      if (without.length < current.length) return without;
      if (current.length >= COMPARE_LIMIT) return current;
      return [...current, { threadId: run.thread_id, filename: run.source_filename }];
    });
  }

  const loading = settledQuery !== query;
  const rows = page?.items ?? [];
  const total = page?.total ?? 0;
  // Everything below describes the rows actually on screen, so it reads the
  // offset the server echoed back — not the local `offset` state, which moves
  // the instant Next is clicked and would otherwise label the still-visible
  // previous page "Showing 26–50".
  const shownOffset = page?.offset ?? 0;
  const showingFrom = total === 0 ? 0 : shownOffset + 1;
  const showingTo = shownOffset + rows.length;
  const hasPrev = shownOffset > 0;
  const hasNext = showingTo < total;
  const filtered = Boolean(status || debouncedSearch);

  return (
    <div className="space-y-4" data-region="runs-index">
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="relative flex-1">
          <Search
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
            aria-hidden="true"
          />
          <input
            type="search"
            className={`${FIELD} w-full pl-9`}
            placeholder="Search by protocol filename or run id"
            aria-label="Search past runs"
            value={search}
            onChange={(e) => changeSearch(e.target.value)}
          />
        </div>
        {/* A native select rather than a shadcn Select: it needs no extra
            dependency, and it is the control that already works with a keyboard,
            a screen reader and a phone's native picker. */}
        <select
          className={`${FIELD} sm:w-56`}
          aria-label="Filter by status"
          value={status}
          onChange={(e) => changeStatus(e.target.value as ScreeningStatus | "")}
        >
          <option value="">All statuses</option>
          {RUN_STATUSES.map((value) => (
            <option key={value} value={value}>
              {statusLabel(value)}
            </option>
          ))}
        </select>
      </div>

      {held.length > 0 && <CompareBar held={held} onClear={() => setHeld([])} />}

      {error && (
        <Card className="border-destructive/40 bg-destructive/10" role="alert">
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent>
          {loading && page === null && !error ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-5/6" />
            </div>
          ) : rows.length === 0 && !error ? (
            <div
              className="text-muted-foreground flex flex-col items-center gap-2 py-8 text-center text-sm"
              data-region="runs-empty"
            >
              <History className="size-5" aria-hidden="true" />
              {filtered ? (
                <span>No runs match this filter.</span>
              ) : (
                <span>
                  No screenings yet.{" "}
                  <Link href="/" className="text-primary underline-offset-4 hover:underline">
                    Upload a protocol
                  </Link>{" "}
                  to start one.
                </span>
              )}
            </div>
          ) : (
            // The table scrolls inside its own container so a narrow viewport
            // never scrolls the whole page sideways.
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8">
                      {/* The column is a strip of checkboxes whose own labels say
                          what they do; a visible header would only repeat them. */}
                      <span className="sr-only">Hold for comparison</span>
                    </TableHead>
                    <TableHead>Protocol</TableHead>
                    <TableHead>Status</TableHead>
                    {/* "Structured", not "Criteria", since #93 put a second
                        criteria column beside it: this one counts what the parser
                        turned into criteria, while Checkable's total also includes
                        the sentences it could not. Two columns headed as if they
                        counted the same thing would read as a contradiction —
                        "Criteria 6" next to "5 of 8". */}
                    <TableHead className="text-right" title="Criteria the parser structured">
                      Structured
                    </TableHead>
                    {/* Screenability (#93): two runs with the same criteria count
                        are not comparable if one of them could only check half of
                        them, and this column is the only place that shows up. */}
                    <TableHead
                      className="text-right"
                      title="Criteria this run could actually check, of every criterion the protocol yielded"
                    >
                      Checkable
                    </TableHead>
                    <TableHead className="text-right">Matches</TableHead>
                    <TableHead>Started</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((run) => {
                    const isHeld = held.some((entry) => entry.threadId === run.thread_id);
                    return (
                      <TableRow key={run.thread_id} data-status={run.status} data-held={isHeld}>
                        <TableCell>
                          {/* A native checkbox, like the status filter's native
                            select: it needs no extra dependency and is already
                            what works with a keyboard, a screen reader and a
                            phone. */}
                          <input
                            type="checkbox"
                            className="accent-primary size-4 disabled:cursor-not-allowed disabled:opacity-40"
                            checked={isHeld}
                            disabled={!isHeld && held.length >= COMPARE_LIMIT}
                            // The filename, not "this row": the accessible name has
                            // to identify the run when read out of context, and it
                            // is what the compare bar will name back.
                            aria-label={`Hold ${run.source_filename} for comparison`}
                            title={
                              !isHeld && held.length >= COMPARE_LIMIT
                                ? "Two runs are already held — clear one to pick a different pair."
                                : undefined
                            }
                            onChange={() => toggleHeld(run)}
                          />
                        </TableCell>
                        <TableCell className="font-medium">
                          {/* The filename is the link: it's the thing a
                            coordinator recognizes, and it gives the row a
                            target big enough to hit on a phone. */}
                          <Link
                            href={runHref(run.thread_id)}
                            className="hover:text-primary underline-offset-4 hover:underline"
                          >
                            {run.source_filename}
                          </Link>
                          <span className="text-muted-foreground block font-mono text-xs">
                            {run.thread_id}
                          </span>
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusVariant(run.status)}>
                            {statusLabel(run.status)}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {run.criteria_count}
                        </TableCell>
                        <CoverageCell coverage={run.coverage} />
                        <TableCell className="text-right tabular-nums">{run.match_count}</TableCell>
                        <TableCell className="text-muted-foreground whitespace-nowrap">
                          {formatTimestamp(run.created_at)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Rendered whenever there is more than one page's worth, so the count is
          visible even on the last page. */}
      {(hasPrev || hasNext) && (
        <div className="flex items-center justify-between gap-3" data-region="runs-pagination">
          <p className="text-muted-foreground text-sm" aria-live="polite">
            Showing {showingFrom}–{showingTo} of {total}
          </p>
          <div className="flex gap-2">
            {/* Both step from `shownOffset` — the page on screen — rather than
                from the requested offset, so they always move exactly one page
                from what the user is looking at. */}
            <Button
              variant="outline"
              onClick={() => setOffset(Math.max(0, shownOffset - PAGE_SIZE))}
              disabled={!hasPrev || loading}
            >
              <ChevronLeft aria-hidden="true" />
              Previous
            </Button>
            <Button
              variant="outline"
              onClick={() => setOffset(shownOffset + PAGE_SIZE)}
              disabled={!hasNext || loading}
            >
              Next
              <ChevronRight aria-hidden="true" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * One row's screenability (#93): how many of the criteria the protocol yielded
 * this run could actually check, and that as a share.
 *
 * Both figures come off the row itself — the API denormalizes them into the store
 * when a run reaches a terminal frame and resolves the percentage server-side, so
 * this cell divides nothing and cannot disagree with the panel on the run's own
 * page. A run with no extraction (uploaded but never streamed, or still parsing)
 * has nothing to be a share of and shows an em dash rather than "0%", which would
 * read as a failed screening.
 */
function CoverageCell({ coverage }: { coverage?: CoverageSummary }) {
  if (!coverage || coverage.criteria === 0) {
    return (
      <TableCell className="text-muted-foreground text-right">
        <span aria-hidden="true">—</span>
        <span className="sr-only">Not scored</span>
      </TableCell>
    );
  }
  const partial = coverage.checkable < coverage.criteria;
  return (
    <TableCell
      className="text-right tabular-nums"
      title={`${coverage.checkable} of ${coverage.criteria} criteria could be checked`}
    >
      {/* The fraction, not the percentage alone: "14 of 20" is what a coordinator
          acts on, and 70% of a two-criterion protocol is a different fact from 70%
          of forty. The share follows it as the scannable column. */}
      <span className={cn(partial && "text-status-warn")}>
        {coverage.checkable} of {coverage.criteria}
      </span>
      <span className="text-muted-foreground block text-xs">{formatShare(coverage.score)}</span>
    </TableCell>
  );
}

/**
 * The staging area for a comparison (#59): which runs are held, and the way in.
 *
 * Held runs are named by filename rather than counted ("2 selected"): the two ids
 * can be picked pages apart, and a coordinator about to read a diff of two
 * protocol versions needs to see *which* two they are holding before they commit
 * to the page. `aria-live` because this bar appears — and rewords itself — in
 * response to a checkbox further down the table, which is not where the reader is
 * looking.
 *
 * Compare is a link, not a fetch: the comparison page owns the request, so the
 * result is bookmarkable and shareable rather than a state only this table can
 * reach.
 */
function CompareBar({ held, onClear }: { held: Held[]; onClear: () => void }) {
  const [first, second] = held;
  const ready = held.length === COMPARE_LIMIT;
  // Two runs of the *same* protocol is the case this feature exists for, so the
  // two filenames are usually identical and naming them alone would read as
  // "comparing x against x". When they collide, the head of each run id
  // disambiguates them — the same prefix the rows show under the filename.
  const ambiguous = ready && first.filename === second.filename;
  const name = (run: Held) =>
    ambiguous ? `${run.filename} (${run.threadId.slice(0, 8)})` : run.filename;

  return (
    <Card data-region="runs-compare-bar">
      <CardContent className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        <GitCompare className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
        <p className="min-w-0 flex-1" aria-live="polite">
          {ready ? (
            <>
              Comparing <span className="font-medium">{name(first)}</span> against{" "}
              <span className="font-medium">{name(second)}</span>.
            </>
          ) : (
            <>
              Holding <span className="font-medium">{first.filename}</span> — pick a second run to
              compare it against.
            </>
          )}
        </p>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClear}>
            Clear
          </Button>
          {/* Rendered as the anchor rather than wrapping one, so there is a single
              focusable control. Disabled until there are two runs: a link to a
              comparison of one run has nowhere to go. */}
          {ready ? (
            <Button render={<Link href={compareHref(first.threadId, second.threadId)} />}>
              Compare
            </Button>
          ) : (
            <Button disabled>Compare</Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
