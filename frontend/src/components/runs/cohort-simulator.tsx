"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, RotateCcw, SlidersHorizontal, Wand2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch, problemDetail } from "@/lib/api";
import { FIELD } from "@/lib/field";
import { formatCount } from "@/lib/metrics";
import { readEventStream } from "@/lib/sse";
import { cn } from "@/lib/utils";
import type {
  CohortAttrition,
  CriterionAttrition,
  CriterionOverride,
  CriterionThreshold,
  Simulation,
} from "@/types";

/**
 * What-if threshold simulation (#95) — move a bound, watch the cohort move.
 *
 * The attrition panel above this says eGFR ≥ 60 excludes 41 of 100 and that
 * relaxing it would recover 14. The question that provokes is *and if it were
 * 50?*, and until this existed the only way to answer it was to edit the
 * criteria, re-run the Critic, approve again and re-score every patient — minutes
 * of work and a rewritten run, to test a number a coordinator wanted to try three
 * of.
 *
 * Every figure comes from `POST /api/screenings/{id}/simulate`, which re-applies
 * the moved comparison to the values the run already recorded: no LLM call, no
 * patient record read, and nothing written. That is why this can recompute on
 * every drag rather than behind a "run simulation" button — the interaction *is*
 * the feature, and a panel that made you commit before showing you the answer
 * would be the re-run it replaces.
 *
 * Two editorial decisions worth knowing:
 *
 * **The comparison is fixed; only the number moves.** A criterion's operator is
 * what it *means* — changing `>=` to `<=` is an edit to the protocol's reading,
 * not a question about its bound — so it is shown and not offered. The API accepts
 * one because the promoted payload has to carry it.
 *
 * **Nothing here is applied until it is promoted.** The simulated column is a
 * projection; promoting it is the ordinary `PATCH /criteria` call, which re-runs
 * the Critic and returns the run to the approval gate. A threshold that reached
 * the criteria without passing the Critic would be exactly the hole the gate
 * exists to close.
 */
export function CohortSimulator({
  threadId,
  attrition,
  revision,
  promotable,
  onPromoted,
}: {
  threadId: string;
  attrition?: CohortAttrition;
  revision: number;
  /** False once a run has been rejected — the API refuses further edits (#91). */
  promotable: boolean;
  onPromoted: () => void;
}) {
  const movable = (attrition?.criteria ?? []).filter(hasThreshold);
  const [overrides, setOverrides] = useState<Record<string, CriterionOverride>>({});
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [promoting, setPromoting] = useState(false);
  const [promoted, setPromoted] = useState<string | null>(null);

  // Half-typed rows are held back rather than sent. An empty box reads as NaN,
  // not as 0 (see `toNumber`) — simulating a threshold of zero because someone
  // selected "65" and pressed delete would answer a question nobody asked, and
  // the API would refuse it anyway.
  const active = Object.values(overrides).filter(isComplete);

  useEffect(() => {
    // Nothing to ask about. Clearing the last override already dropped the
    // result (see `clear`) — doing it here instead would be a setState in an
    // effect body, and one more render than the interaction needs.
    if (active.length === 0) return;
    // Debounced, and cancelled by the cleanup on the next keystroke: a reviewer
    // dragging through twenty values should cost one request at the end of the
    // gesture, not twenty the last of which might land first.
    let live = true;
    const timer = setTimeout(async () => {
      try {
        const response = await apiFetch(
          `/api/screenings/${encodeURIComponent(threadId)}/simulate`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ overrides: active }),
          }
        );
        if (!live) return;
        if (!response.ok) {
          // 409 for a run with no cohort, 422 for a criterion that can't be
          // simulated — the API's own wording is more use than ours.
          setError(await problemDetail(response, "Could not simulate this change"));
          setSimulation(null);
          return;
        }
        setError(null);
        setSimulation((await response.json()) as Simulation);
      } catch {
        if (live) setError("Could not reach the server.");
      }
    }, 300);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // `active` is rebuilt every render, so the effect keys on its serialized form
    // — otherwise every unrelated re-render would fire another request.
  }, [threadId, JSON.stringify(active)]); // eslint-disable-line react-hooks/exhaustive-deps

  function setBound(row: CriterionAttrition, patch: Partial<CriterionOverride>) {
    const threshold = row.threshold;
    if (!threshold) return;
    // Seeded from the criterion's own bound, so the first nudge of one field
    // submits the others as the run actually has them rather than as zeroes.
    const asScored: CriterionOverride = {
      key: row.key,
      operator: threshold.operator,
      value: threshold.value,
      value_high: threshold.value_high,
    };
    setOverrides((previous) => ({
      ...previous,
      [row.key]: { ...asScored, ...previous[row.key], ...patch },
    }));
  }

  function clear(key: string) {
    const rest = Object.fromEntries(Object.entries(overrides).filter(([entry]) => entry !== key));
    setOverrides(rest);
    // The last override going away leaves nothing to show, and a stale simulated
    // column beside a reset control would read as the answer to a question the
    // reviewer has just withdrawn.
    if (Object.keys(rest).length === 0) {
      setSimulation(null);
      setError(null);
    }
  }

  async function promote() {
    if (!simulation) return;
    setPromoting(true);
    setError(null);
    let response: Response;
    try {
      response = await apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/criteria`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_revision: simulation.criteria_revision,
          criteria: simulation.criteria,
        }),
      });
    } catch {
      // A dropped connection before the request landed. Without this the button
      // stays on "Applying…" forever with every control disabled and nothing said.
      setError("Could not reach the server. Nothing was applied.");
      setPromoting(false);
      return;
    }
    if (!response.ok) {
      // 409 if someone else edited the run since this simulation was computed, or
      // if it has been rejected in the meantime. The run is untouched either way.
      setError(await problemDetail(response, "Could not apply these thresholds"));
      setPromoting(false);
      return;
    }
    // The PATCH re-runs the Critic over SSE, exactly as the criteria editor's
    // "Save & re-run" does. We only need to know how it ended.
    let outcome = "The thresholds were applied and the run is back at the approval gate.";
    let escalated = false;
    try {
      await readEventStream(response, (message) => {
        if (message.node === "human_escalation") {
          escalated = true;
          return false;
        }
        if (message.node === "__error__") {
          outcome = message.message ?? "The re-run failed.";
          return true;
        }
        if (message.node === "__interrupt__" || message.node === "__end__") {
          if (escalated) {
            outcome =
              "The thresholds were applied, but the Critic rejects them and the run escalated " +
              "— open it in the review queue to see why.";
          }
          return true;
        }
        return false;
      });
    } catch {
      outcome =
        "The thresholds were applied, but the connection dropped before the re-run reported " +
        "back. Reload to see where the run ended up.";
    }
    setPromoted(outcome);
    setOverrides({});
    setSimulation(null);
    setPromoting(false);
    onPromoted();
  }

  // Nothing numeric to move: a protocol of purely categorical criteria, or a run
  // that never scored a cohort. The panel disappears rather than offering a
  // control with nothing behind it — *unless* a promotion just landed, which is
  // itself how the rows go away (the cohort is discarded, so there is no
  // attrition left to move). Unmounting then would take the outcome with it, and
  // the one outcome worth reading is the one where the Critic rejected the
  // thresholds a reviewer had just talked themselves into.
  if (movable.length === 0 && !promoted) return null;

  return (
    <Card data-region="cohort-simulator">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <SlidersHorizontal className="text-muted-foreground size-4" aria-hidden="true" />
          What if the thresholds moved?
        </CardTitle>
        <p className="text-muted-foreground text-xs">
          {movable.length > 0
            ? "Re-scores the patients this run already screened against a different bound. " +
              "Nothing is applied, no patient record is re-read, and the extraction is untouched " +
              "until you promote it."
            : "This run has no scored cohort to simulate against — approve it at the gate to " +
              "screen patients under the new thresholds."}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {movable.map((row) => (
          <ThresholdControl
            key={row.key}
            row={row}
            override={overrides[row.key]}
            disabled={promoting}
            onChange={(patch) => setBound(row, patch)}
            onReset={() => clear(row.key)}
            echoed={simulation?.overrides.find((entry) => entry.key === row.key)}
          />
        ))}

        {error && (
          <p className="text-destructive text-sm" role="alert">
            {error}
          </p>
        )}

        {promoted && (
          <p
            className="border-primary/40 bg-primary/10 rounded-lg border p-3 text-sm"
            role="status"
            data-region="simulation-promoted"
          >
            {promoted}
          </p>
        )}

        {simulation && <Outcome simulation={simulation} />}

        {simulation && promotable && (
          <div className="flex flex-wrap items-center gap-3 border-t pt-3 text-sm">
            <span className="text-muted-foreground flex-1">
              Promoting rewrites these thresholds into the run&apos;s criteria as revision{" "}
              {revision + 1}, re-runs the compliance Critic over them, and returns the run to the
              approval gate. The cohort above is discarded — it was scored against the old bounds.
            </span>
            <Button className="shrink-0" disabled={promoting} onClick={promote}>
              <Wand2 aria-hidden="true" />
              {promoting ? "Applying…" : "Promote to a criteria edit"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * A number input's value as the API wants it.
 *
 * An empty box is NaN rather than 0, exactly as in the criteria editor: a
 * threshold is a clinical decision, and reading a blank field as "zero" would
 * silently simulate a criterion every patient passes.
 */
function toNumber(raw: string): number {
  return raw.trim() === "" ? Number.NaN : Number(raw);
}

/** Whether an override has every bound its operator needs. */
function isComplete(override: CriterionOverride): boolean {
  if (Number.isNaN(override.value)) return false;
  return !(
    override.operator === "between" &&
    (override.value_high === null || Number.isNaN(override.value_high))
  );
}

/** Narrows a row to one with a bound, so `row.threshold` is non-null downstream. */
function hasThreshold(
  row: CriterionAttrition
): row is CriterionAttrition & { threshold: CriterionThreshold } {
  return Boolean(row.threshold);
}

/**
 * One criterion's bound: a slider where the cohort's own span can bound it, a
 * number box always, and whatever the API made of the value underneath.
 *
 * The slider spans `observed_min`..`observed_max` — the values these patients
 * actually have — so both of its ends are the trivial answers (excludes nobody,
 * excludes everybody) and every position between them is a threshold that changes
 * something. A range invented from the current value would promise neither.
 */
function ThresholdControl({
  row,
  override,
  disabled,
  onChange,
  onReset,
  echoed,
}: {
  row: CriterionAttrition & { threshold: CriterionThreshold };
  override?: CriterionOverride;
  disabled: boolean;
  onChange: (patch: Partial<CriterionOverride>) => void;
  onReset: () => void;
  echoed?: Simulation["overrides"][number];
}) {
  const { threshold } = row;
  const value = override?.value ?? threshold.value;
  const high = override?.value_high ?? threshold.value_high;
  const moved = Boolean(override);
  const span = sliderSpan(threshold);

  return (
    <div className="space-y-1.5" data-region="simulator-criterion" data-criterion={row.key}>
      <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="min-w-0 flex-1">
          {row.label}{" "}
          <span className="text-muted-foreground text-xs">
            {row.kind === "exclusion" ? "exclusion" : "inclusion"} · excludes {row.excluded}
          </span>
        </span>
        {moved && (
          <Button variant="ghost" size="sm" disabled={disabled} onClick={onReset}>
            <RotateCcw aria-hidden="true" />
            Reset
          </Button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {/* The operator is shown, not offered: see the panel's docstring. */}
        <span className="text-muted-foreground w-16 shrink-0 text-right font-mono text-xs">
          {threshold.operator}
        </span>
        <input
          type="number"
          step="any"
          className={`${FIELD} w-24`}
          aria-label={`${row.label} — simulated threshold`}
          // Flagged rather than blanked while the box is empty: the simulated
          // column below still shows the last complete answer, and the invalid
          // ring is what says it does not describe what is on screen right now.
          aria-invalid={Number.isNaN(value)}
          value={Number.isNaN(value) ? "" : String(value)}
          disabled={disabled}
          onChange={(e) => onChange({ value: toNumber(e.target.value) })}
        />
        {threshold.operator === "between" && (
          <input
            type="number"
            step="any"
            className={`${FIELD} w-24`}
            aria-label={`${row.label} — simulated upper bound`}
            aria-invalid={high === null || Number.isNaN(high)}
            value={high === null || Number.isNaN(high) ? "" : String(high)}
            disabled={disabled}
            onChange={(e) => onChange({ value_high: toNumber(e.target.value) })}
          />
        )}
        <span className="text-muted-foreground shrink-0 text-xs">{threshold.unit}</span>
        {span && (
          <input
            type="range"
            className="accent-primary min-w-40 flex-1"
            aria-label={`${row.label} — drag the threshold`}
            min={span.min}
            max={span.max}
            step={span.step}
            value={clamp(value, span)}
            disabled={disabled}
            onChange={(e) => onChange({ value: Number(e.target.value) })}
          />
        )}
      </div>

      {span ? (
        <p className="text-muted-foreground text-xs">
          These patients range from {span.min} to {span.max} {threshold.unit}.
        </p>
      ) : (
        <p className="text-muted-foreground text-xs">
          No value for this attribute is on file for any of these patients — either none of their
          records carried one, or the run predates the values being recorded — so this threshold can
          be typed but not dragged.
        </p>
      )}

      {echoed && echoed.unavailable > 0 && (
        <p className="text-status-warn text-xs" role="status">
          {formatCount(echoed.unavailable, "patient")} could not be re-checked — this run did not
          record the values it compared them against, so they are counted as they were.
        </p>
      )}

      {echoed?.findings.map((finding) => (
        <p
          key={`${echoed.key}|${finding.rule_id}`}
          className="border-status-warn/40 bg-status-warn-soft flex items-start gap-2 rounded-lg border p-2 text-xs"
          role="alert"
          data-region="simulation-finding"
        >
          <AlertTriangle className="text-status-warn mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <span>
            {finding.explanation || finding.message}{" "}
            <span className="text-muted-foreground font-mono">({finding.rule_id})</span>
          </span>
        </p>
      ))}
    </div>
  );
}

/**
 * The two cohorts side by side, and what moved between them.
 *
 * `current` is the API's own copy of the attrition block the panel above already
 * rendered, so the left column is the same derivation the reviewer is comparing
 * against rather than a second one that could drift from it.
 */
function Outcome({ simulation }: { simulation: Simulation }) {
  const { current, simulated, delta } = simulation;
  const bindingNow = simulated.criteria.find((row) => row.excluded > 0);

  return (
    <div className="border-t pt-3" data-region="simulation-outcome">
      <table className="w-full text-sm">
        <caption className="text-muted-foreground pb-2 text-left text-xs">
          {formatCount(current.totals.patients, "patient")} re-scored against the thresholds above.
        </caption>
        <thead>
          <tr className="text-muted-foreground text-xs">
            <th className="py-1 text-left font-medium">Bucket</th>
            <th className="py-1 text-right font-medium">Now</th>
            <th className="py-1 text-right font-medium">Simulated</th>
            <th className="py-1 text-right font-medium">Change</th>
          </tr>
        </thead>
        <tbody>
          {(["eligible", "review", "ineligible"] as const).map((bucket) => (
            <tr key={bucket} className="border-t" data-bucket={bucket}>
              <td className="py-1.5">{BUCKET_LABELS[bucket]}</td>
              <td className="py-1.5 text-right tabular-nums">{current.totals[bucket]}</td>
              <td className="py-1.5 text-right font-medium tabular-nums">
                {simulated.totals[bucket]}
              </td>
              <td
                className={cn(
                  "py-1.5 text-right tabular-nums",
                  delta[bucket] === 0 && "text-muted-foreground",
                  delta[bucket] > 0 && bucket === "eligible" && "text-status-pass",
                  delta[bucket] < 0 && bucket === "eligible" && "text-status-fail"
                )}
              >
                {signed(delta[bucket])}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* What a coordinator does next. Once the criterion they dragged stops
          binding, the next one along is the conversation to have with the
          sponsor — and it is one row of a table they would otherwise have to
          re-read to find. */}
      {bindingNow && (
        <p className="text-muted-foreground pt-2 text-xs">
          <span className="text-foreground">{bindingNow.label}</span> would then be the most
          restrictive criterion, excluding {formatCount(bindingNow.excluded, "patient")}.
        </p>
      )}
      {!bindingNow && (
        <p className="text-muted-foreground pt-2 text-xs">
          No criterion would exclude anyone under these thresholds.
        </p>
      )}
    </div>
  );
}

const BUCKET_LABELS = {
  eligible: "Eligible",
  review: "Needs review",
  ineligible: "Ineligible",
} as const;

/** `+3`, `-2`, `0` — the sign is the whole content of the column. */
function signed(delta: number): string {
  return delta > 0 ? `+${delta}` : String(delta);
}

type Span = { min: number; max: number; step: number };

/**
 * The slider's range, or null when this run recorded no values to bound it with.
 *
 * Padded outward to whole steps so both extremes are reachable: a slider whose
 * maximum is exactly the highest patient value cannot express "exclude everyone",
 * which is one of the two answers a reviewer checks first.
 */
function sliderSpan(threshold: CriterionThreshold): Span | null {
  const { observed_min: low, observed_max: high } = threshold;
  if (low === null || high === null) return null;
  const width = high - low;
  // Coarse for the labs that are counted in tens or hundreds, fine for the ones
  // measured in single digits (ECOG, ANC) where a step of 1 is the whole range.
  const step = width > 20 ? 1 : width > 2 ? 0.1 : 0.01;
  return { min: floorTo(low - step, step), max: ceilTo(high + step, step), step };
}

function floorTo(value: number, step: number): number {
  return Number((Math.floor(value / step) * step).toFixed(2));
}

function ceilTo(value: number, step: number): number {
  return Number((Math.ceil(value / step) * step).toFixed(2));
}

/** Keeps the slider's thumb on the track when a typed value runs past its ends. */
function clamp(value: number, span: Span): number {
  if (Number.isNaN(value)) return span.min;
  return Math.min(span.max, Math.max(span.min, value));
}
