"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronLeft, ChevronRight, History, Search } from "lucide-react";
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
import { RUN_STATUSES, formatTimestamp, runHref, statusLabel, statusVariant } from "@/lib/runs";
import type { ScreeningPage, ScreeningStatus } from "@/types";

const PAGE_SIZE = 25;

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
                    <TableHead>Protocol</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Criteria</TableHead>
                    <TableHead className="text-right">Matches</TableHead>
                    <TableHead>Started</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((run) => (
                    <TableRow key={run.thread_id} data-status={run.status}>
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
                        <Badge variant={statusVariant(run.status)}>{statusLabel(run.status)}</Badge>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {run.criteria_count}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{run.match_count}</TableCell>
                      <TableCell className="text-muted-foreground whitespace-nowrap">
                        {formatTimestamp(run.created_at)}
                      </TableCell>
                    </TableRow>
                  ))}
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
