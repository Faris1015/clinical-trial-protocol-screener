"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronLeft, ChevronRight, ScrollText, Search } from "lucide-react";
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
import { AuditExport } from "@/components/audit/audit-export";
import { useAuth } from "@/components/AuthProvider";
import { apiFetch, problemDetail } from "@/lib/api";
import {
  actionLabel,
  actionVariant,
  actionsFor,
  dayBound,
  decisionHref,
  decisionSubject,
} from "@/lib/audit";
import { FIELD } from "@/lib/field";
import { formatTimestamp } from "@/lib/runs";
import type { AuditAction, AuditEntry, AuditPage } from "@/types";

const PAGE_SIZE = 25;

// The run filter is a pasted thread id, and pasting one is a single event — but
// it is also a text box someone can type into, and the endpoint is rate limited
// on the read bucket. Same debounce the runs index uses on its search field.
const FILTER_DEBOUNCE_MS = 300;

/**
 * The org-wide audit log (#98): every approval, rejection, criteria revision and
 * escalation this instance has recorded, newest first.
 *
 * Filtering and paging happen server-side, over an index written as decisions
 * happen — not over a client-side copy, and not by scanning checkpoints. The list
 * grows for the life of the deployment, and the entry an auditor is looking for is
 * by definition not on the page they would be handed.
 *
 * What a reader sees is scoped by their role, and scoped *by the server*: an admin
 * reads the whole org, a reviewer reads their own decisions. The page says which
 * of the two it is showing rather than leaving it to be inferred from the role —
 * and it reads that from the response's own `scope`, so the sentence can never
 * disagree with the rows under it.
 */
export function AuditIndex() {
  const { principal } = useAuth();
  const [action, setAction] = useState<AuditAction | "">("");
  const [actor, setActor] = useState("");
  const [run, setRun] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [debounced, setDebounced] = useState({ actor: "", run: "" });
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<AuditPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settledQuery, setSettledQuery] = useState<string | null>(null);

  // Only an admin can narrow by actor — the API answers a reviewer asking for
  // someone else's decisions with a 403, so offering them the box would be
  // offering a control whose only outcome is an error.
  const isAdmin = principal?.role === "admin";

  useEffect(() => {
    const timer = setTimeout(() => setDebounced({ actor, run }), FILTER_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [actor, run]);

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (action) params.set("action", action);
    if (isAdmin && debounced.actor) params.set("actor", debounced.actor);
    if (debounced.run) params.set("thread_id", debounced.run);
    // Sent as instants, not as the bare days the inputs emit: the rows are
    // rendered in the reader's timezone and the API reads a bare day as UTC, so
    // west of UTC the two would disagree about which day a decision belongs to.
    if (from) params.set("from", dayBound(from, false));
    if (to) params.set("to", dayBound(to, true));
    return params.toString();
  }, [offset, action, isAdmin, debounced, from, to]);

  // The offset of the page on screen, as a ref as well as derived below: the
  // failure path has to read it without making the effect depend on the loaded
  // page. Same arrangement as the runs index.
  const shownOffsetRef = useRef(0);

  useEffect(() => {
    let active = true;

    const failed = (detail: string) => {
      // Leave the previous page under the error rather than blanking the table —
      // a transient 429 must not look like "nobody has decided anything" — and put
      // the offset back to the page still displayed, so a failed "Next" does not
      // leave the next click skipping the page that failed.
      setError(detail);
      setOffset(shownOffsetRef.current);
      setSettledQuery(query);
    };

    apiFetch(`/api/audit?${query}`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          const detail = await problemDetail(response, "Could not load the audit log");
          if (!active) return;
          failed(detail);
          return;
        }
        const body = (await response.json()) as AuditPage;
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

  // Changing a filter resets paging in the handler rather than in an effect
  // watching the filters: staying on offset 50 of a narrower result set shows an
  // empty page that reads as "no decisions match".
  function change<T>(set: (value: T) => void) {
    return (value: T) => {
      set(value);
      setOffset(0);
      shownOffsetRef.current = 0;
    };
  }

  const loading = settledQuery !== query;
  const rows = page?.items ?? [];
  const total = page?.total ?? 0;
  const shownOffset = page?.offset ?? 0;
  const showingFrom = total === 0 ? 0 : shownOffset + 1;
  const showingTo = shownOffset + rows.length;
  const hasPrev = shownOffset > 0;
  const hasNext = showingTo < total;
  const filtered = Boolean(action || debounced.actor || debounced.run || from || to);
  // The server's own answer, not `isAdmin`: it is the scope that produced these
  // rows, so the sentence and the table cannot disagree.
  const scopedToSelf = Boolean(page && page.scope.actor && !isAdmin);

  return (
    <div className="space-y-4" data-region="audit-index">
      <div className="flex flex-col gap-2 lg:flex-row lg:flex-wrap">
        <select
          className={`${FIELD} lg:w-48`}
          aria-label="Filter by decision"
          value={action}
          onChange={(e) => change(setAction)(e.target.value as AuditAction | "")}
        >
          <option value="">All decisions</option>
          {actionsFor(isAdmin).map((value) => (
            <option key={value} value={value}>
              {actionLabel(value)}
            </option>
          ))}
        </select>

        {isAdmin && (
          <div className="relative flex-1 lg:min-w-56">
            <Search
              className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              aria-hidden="true"
            />
            <input
              type="search"
              className={`${FIELD} w-full pl-9`}
              placeholder="Filter by staff email"
              aria-label="Filter by staff email"
              value={actor}
              onChange={(e) => change(setActor)(e.target.value)}
            />
          </div>
        )}

        <input
          type="search"
          className={`${FIELD} flex-1 lg:min-w-56`}
          placeholder="Filter by run id"
          aria-label="Filter by run id"
          value={run}
          onChange={(e) => change(setRun)(e.target.value)}
        />

        {/* Native date inputs, like the native select above: they need no extra
            dependency and are already what works with a keyboard, a screen reader
            and a phone's own picker. The API takes a bare day and covers all of
            it, which is exactly what these emit. */}
        <div className="flex flex-1 items-center gap-2 lg:flex-none">
          <input
            type="date"
            className={`${FIELD} flex-1`}
            aria-label="Decisions from"
            value={from}
            max={to || undefined}
            onChange={(e) => change(setFrom)(e.target.value)}
          />
          <span className="text-muted-foreground text-sm">to</span>
          <input
            type="date"
            className={`${FIELD} flex-1`}
            aria-label="Decisions to"
            value={to}
            min={from || undefined}
            onChange={(e) => change(setTo)(e.target.value)}
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* How far this page reaches, stated rather than left to be inferred from
            the reader's role — and read off the response's own `scope`, so the
            sentence cannot disagree with the rows under it. */}
        <p className="text-muted-foreground text-sm">
          {scopedToSelf
            ? "Your own decisions. Ask an admin for the org-wide log."
            : "Every decision, across every run."}
        </p>
        {/* The export takes the filters on screen, so an auditor downloads the
            page they are looking at rather than a second, wider query. */}
        <AuditExport query={query} />
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
              data-region="audit-empty"
            >
              <ScrollText className="size-5" aria-hidden="true" />
              <span>
                {filtered
                  ? "No decisions match this filter."
                  : "No decisions recorded yet. Approvals, rejections, criteria revisions and escalations appear here as they happen."}
              </span>
            </div>
          ) : (
            // Scrolls inside its own container so a narrow viewport never scrolls
            // the whole page sideways.
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>When</TableHead>
                    <TableHead>Decision</TableHead>
                    <TableHead>Who</TableHead>
                    {/* Not "Run": since #97 a decision can be about a compliance
                        rule instead, and a column headed "Run" over a rule id
                        would read as a mislabelled cell rather than a second
                        kind of subject. */}
                    <TableHead>Subject</TableHead>
                    <TableHead>Detail</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <AuditRow key={row.id} entry={row} />
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {(hasPrev || hasNext) && (
        <div className="flex items-center justify-between gap-3" data-region="audit-pagination">
          <p className="text-muted-foreground text-sm" aria-live="polite">
            Showing {showingFrom}–{showingTo} of {total}
          </p>
          <div className="flex gap-2">
            {/* Both step from the page on screen rather than from the requested
                offset, so they always move exactly one page from what is visible. */}
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
 * One decision.
 *
 * The run cell is the link, and for a criteria revision it links to that
 * revision's before/after diff rather than to the run at large (AC 3) — the
 * question an auditor has about a revision is *what changed*, and landing them on
 * the answer is the difference between an index and a list of pointers.
 */
function AuditRow({ entry }: { entry: AuditEntry }) {
  const system = !entry.actor.includes("@");
  return (
    <TableRow data-action={entry.action}>
      <TableCell className="text-muted-foreground whitespace-nowrap">
        {formatTimestamp(entry.occurred_at)}
      </TableCell>
      <TableCell>
        <Badge variant={actionVariant(entry.action)}>{entry.label}</Badge>
        {entry.revision > 0 && (
          <span className="text-muted-foreground block text-xs">revision {entry.revision}</span>
        )}
      </TableCell>
      <TableCell>
        {/* An escalation is the pipeline's act, not a person's, and it is named as
            such rather than shown as a machine-looking identifier in a column of
            colleagues' addresses. */}
        {system ? (
          <span className="text-muted-foreground">TrialGate pipeline</span>
        ) : (
          <>
            <span className="font-medium break-all">{entry.actor}</span>
            {entry.actor_role && (
              <span className="text-muted-foreground block text-xs">{entry.actor_role}</span>
            )}
          </>
        )}
      </TableCell>
      {/* What the decision was about. A run names the protocol it screened; a
          rule mutation (#97) names the rule, and links to it on the rules page —
          a rule entry has no run, and pointing one at `/runs/` would be a 404
          dressed as a link. */}
      <TableCell>
        <Link
          href={decisionHref(entry)}
          className="hover:text-primary font-medium underline-offset-4 hover:underline"
        >
          {entry.subject_kind === "rule" ? "Compliance rule" : entry.source_filename || "Screening"}
        </Link>
        <span className="text-muted-foreground block font-mono text-xs break-all">
          {decisionSubject(entry)}
        </span>
      </TableCell>
      {/* The one column that wraps: a rejection carries the reviewer's whole
          reason, which is the record. No minimum width — the table already has
          four columns of fixed-ish content, and forcing a fifth wider than the
          viewport would push exactly this cell off the right edge, where it is
          the last thing a reader would think to scroll for. */}
      <TableCell className="text-muted-foreground text-sm whitespace-normal">
        {entry.detail}
      </TableCell>
    </TableRow>
  );
}
