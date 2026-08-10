"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  AlertTriangle,
  ArrowLeft,
  Ban,
  CheckCircle2,
  CornerUpLeft,
  Play,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { ComplianceFindings } from "@/components/ComplianceFindings";
import { CriteriaDiff } from "@/components/review/criteria-diff";
import { RejectScreening } from "@/components/review/reject-screening";
import { CriteriaEditorSkeleton } from "@/components/skeletons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch, problemDetail } from "@/lib/api";
import {
  CATEGORIES,
  EHR_ATTRIBUTES,
  OPERATORS,
  blankCategorical,
  blankQuantitative,
  demotionText,
} from "@/lib/criteria";
import { FIELD } from "@/lib/field";
import { formatTimestamp, runHref, statusLabel, statusVariant } from "@/lib/runs";
import { readEventStream } from "@/lib/sse";
import type {
  CategoricalCriterion,
  CriteriaSchema,
  QuantitativeCriterion,
  ScreeningState,
} from "@/types";

/** Buckets holding quantitative criteria, and the categorical pair. */
const QUANT_BUCKETS = ["inclusion_quantitative", "exclusion_quantitative"] as const;
const CAT_BUCKETS = ["inclusion_categorical", "exclusion_categorical"] as const;

type QuantBucket = (typeof QUANT_BUCKETS)[number];
type CatBucket = (typeof CAT_BUCKETS)[number];

type Phase = "editing" | "rerunning" | "done";

/**
 * How a finished re-run went. `ok` is not decoration: a re-escalation ends the
 * stream with a plain `__end__` frame, so without it a run the Critic still
 * rejects would be reported in the same green banner as one that passed.
 */
type Outcome = { text: string; ok: boolean };

const SECTION_TITLES: Record<QuantBucket | CatBucket, string> = {
  inclusion_quantitative: "Inclusion criteria · numeric thresholds",
  inclusion_categorical: "Inclusion criteria · categorical",
  exclusion_quantitative: "Exclusion criteria · numeric thresholds",
  exclusion_categorical: "Exclusion criteria · categorical",
};

/**
 * A number input's value as the API wants it.
 *
 * An empty box is NaN rather than 0: a threshold is a clinical decision, and
 * silently reading a blank field as "zero" would submit a criterion every patient
 * passes. `firstProblem` refuses to send a NaN, so the reviewer is told instead.
 */
function toNumber(raw: string): number {
  return raw.trim() === "" ? Number.NaN : Number(raw);
}

/** A number for display: NaN (an unfilled box) shows as empty, not as "NaN". */
function fromNumber(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "";
  return String(value);
}

/**
 * Whether these criteria can be submitted — every numeric bound filled in, every
 * categorical criterion named.
 *
 * The backend would reject a NaN with a 422, but the reviewer has just typed the
 * fields and deserves to be told which one is wrong here rather than after a
 * round-trip that also burns a rate-limit slot.
 */
function firstProblem(criteria: CriteriaSchema): string | null {
  for (const bucket of QUANT_BUCKETS) {
    for (const c of criteria[bucket]) {
      if (Number.isNaN(c.value)) return "Every numeric criterion needs a value.";
      if (c.operator === "between" && (c.value_high === null || Number.isNaN(c.value_high)))
        return "A 'between' criterion needs both an upper and a lower bound.";
      if (!c.unit.trim()) return "Every numeric criterion needs a unit.";
    }
  }
  for (const bucket of CAT_BUCKETS) {
    for (const c of criteria[bucket]) {
      if (!c.value.trim()) return "Every categorical criterion needs a term.";
    }
  }
  return null;
}

/**
 * The human-in-the-loop edit-and-rerun page (#53).
 *
 * The gate used to be approve-only: a reviewer looking at a bad threshold, a
 * hallucinated criterion, or an `unparseable` sentence the Parser gave up on could
 * only approve it anyway or leave the run stuck. This is the other exit — fix the
 * extraction, re-run from the Critic, and see exactly what changed.
 *
 * Every field sits beside the verbatim protocol sentence it came from, because
 * that sentence is the only thing that makes an edit defensible: a reviewer
 * correcting "age >= 180" needs to read what the protocol actually said, not
 * remember it.
 */
export function CriteriaEditor() {
  // `useSearchParams`, not a `/review/[threadId]` segment: static export, see
  // lib/criteria.reviewHref.
  const threadId = useSearchParams().get("id");
  const [state, setState] = useState<ScreeningState | null>(null);
  const [draft, setDraft] = useState<CriteriaSchema | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("editing");
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  // Bumped after a re-run to re-read the checkpoint. A token rather than a
  // callable loader so there is exactly one fetch path — and so the fetch stays
  // inside the effect, where the out-of-order guard lives.
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!threadId) return;
    let active = true;
    apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/state`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          // 404 for an id that never existed, 401 for an expired session — the
          // API's own wording beats ours.
          setError(await problemDetail(response, "Could not load this run"));
          return;
        }
        const body = (await response.json()) as ScreeningState;
        if (!active) return;
        setError(null);
        setState(body);
        // The draft is seeded from the server's copy once per load and the
        // reviewer owns it from then on — re-seeding on every render would throw
        // away their in-progress edits. structuredClone so editing the draft never
        // mutates the loaded state we diff and reset against.
        setDraft(body.values.parsed_criteria ? structuredClone(body.values.parsed_criteria) : null);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [threadId, reloadToken]);

  function patchQuant(bucket: QuantBucket, index: number, patch: Partial<QuantitativeCriterion>) {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            [bucket]: prev[bucket].map((c, i) => (i === index ? { ...c, ...patch } : c)),
          }
        : prev
    );
  }

  function patchCat(bucket: CatBucket, index: number, patch: Partial<CategoricalCriterion>) {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            [bucket]: prev[bucket].map((c, i) => (i === index ? { ...c, ...patch } : c)),
          }
        : prev
    );
  }

  function removeAt(bucket: QuantBucket | CatBucket | "unparseable", index: number) {
    setDraft((prev) =>
      prev ? { ...prev, [bucket]: prev[bucket].filter((_, i) => i !== index) } : prev
    );
  }

  /**
   * Move an `unparseable` sentence into a real bucket, carrying its text along as
   * the new criterion's `source_text` — which is what makes the backend's diff
   * report one "reclassified" entry rather than a delete and an unrelated add.
   */
  function reclassify(index: number, bucket: QuantBucket | CatBucket) {
    setDraft((prev) => {
      if (!prev) return prev;
      const sentence = prev.unparseable[index];
      const next = { ...prev, unparseable: prev.unparseable.filter((_, i) => i !== index) };
      if (bucket === "inclusion_quantitative" || bucket === "exclusion_quantitative") {
        return { ...next, [bucket]: [...prev[bucket], blankQuantitative(sentence)] };
      }
      return { ...next, [bucket]: [...prev[bucket], blankCategorical(sentence)] };
    });
  }

  /**
   * The other direction (#92): a criterion the Parser typed but got wrong goes
   * back to being the sentence it came from.
   *
   * Not the same thing as deleting it. A mis-parsed criterion — "adequate hepatic
   * function" read as an eGFR threshold — is a real eligibility requirement wearing
   * the wrong numbers, and deleting it drops the requirement from the protocol
   * entirely. Demoting keeps the sentence on the record: the exported report and
   * the provenance viewer both render `unparseable`, so a reader sees that the
   * protocol asked for something this run could not screen.
   *
   * One sharp edge, inherited from edit-and-rerun rather than introduced here.
   * The Critic's `must_be_quantitative` rules key off `unparseable`, so demoting
   * a sentence whose topic the protocol also states in prose can legitimately
   * re-reject the run — and a rejected re-run below the retry cap routes back to
   * the *Parser*, which replaces `parsed_criteria` wholesale and takes the
   * demotion with it. Any edit that trips a `reject` rule loses itself the same
   * way (deleting a criterion the protocol demands, moving a threshold out of its
   * plausible range); demotion is only the most likely to. Fixing that means
   * teaching the graph not to re-parse after a human edit, which is #53's
   * routing, not this affordance's.
   */
  function demote(bucket: QuantBucket | CatBucket, index: number) {
    setDraft((prev) => {
      if (!prev) return prev;
      const sentence = demotionText(prev[bucket][index]);
      if (sentence === null) return prev;
      return {
        ...prev,
        [bucket]: prev[bucket].filter((_, i) => i !== index),
        unparseable: [...prev.unparseable, sentence],
      };
    });
  }

  async function rerun() {
    if (!draft || !threadId) return;
    const problem = firstProblem(draft);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    setOutcome(null);
    // Flip before the request so the button is disabled for the whole re-run: a
    // second PATCH would land on revision N+1 and 409, which is safe but reads to
    // the reviewer as if their edit failed.
    setPhase("rerunning");
    const response = await apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/criteria`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_revision: state?.values.criteria_revision ?? 0,
        criteria: draft,
      }),
    });
    if (!response.ok) {
      // Eager-validation errors arrive as JSON before the stream commits: 409 for
      // a stale revision or a run that isn't editable, 422 for a criterion the
      // schema rejects, 429 when every slot is busy. The run is untouched in all
      // of them, so the draft stays on screen to retry or fix.
      setError(await problemDetail(response, "Re-run failed"));
      setPhase("editing");
      return;
    }
    // The re-run streams the Critic (and the escalation node, if it still can't
    // pass) over SSE, exactly like /approve.
    let outcomeText: Outcome | null = null;
    let escalated = false;
    try {
      await readEventStream(response, (message) => {
        if (message.node === "__interrupt__") {
          outcomeText = {
            text: "Compliance checks passed — the run is back at the approval gate.",
            ok: true,
          };
          return true;
        }
        if (message.node === "__error__") {
          outcomeText = { text: message.message ?? "The re-run failed.", ok: false };
          return true;
        }
        // The Critic still rejected the edits and the retry cap is spent, so the
        // graph escalated. It ends with a plain `__end__`, not `__error__` — the run
        // did what it was supposed to — so this flag is the only thing that keeps
        // the banner below from reporting a re-escalation as a clean finish.
        if (message.node === "human_escalation") {
          escalated = true;
          return false;
        }
        if (message.node === "__end__") {
          outcomeText = escalated
            ? {
                text:
                  "The Critic still rejects these criteria, so the run escalated again — " +
                  "its findings are above, and the criteria stay editable.",
                ok: false,
              }
            : { text: "The re-run finished.", ok: true };
          return true;
        }
        return false;
      });
    } catch {
      // The connection dropped mid-re-run. The edits are already in the
      // checkpoint (the PATCH was accepted before the first frame), so this is
      // about not knowing how it ended — hence "reload", not "retry".
      outcomeText = null;
    }
    // A stream that ended without a terminal frame leaves nothing to report, and
    // silence here would read as "nothing happened" for a re-run that did in fact
    // land. Say so instead — and either way, leave `phase` as "done" so the button
    // is usable again rather than stuck disabled.
    setOutcome(
      outcomeText ?? {
        text:
          "Lost the connection before the re-run reported back. Your edits were saved — " +
          "reload to see where the run ended up.",
        ok: false,
      }
    );
    setPhase("done");
    // Re-read the checkpoint rather than trusting the local draft: the server's
    // copy is what the matcher will run against, and it now also carries the new
    // revision and the diff this page renders below.
    setReloadToken((token) => token + 1);
  }

  const backLink = (
    <Link
      href="/review/"
      className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 text-sm"
    >
      <ArrowLeft className="size-4" aria-hidden="true" />
      Back to the review queue
    </Link>
  );

  const alert = (message: string, tone: "error" | "warn" = "error") => (
    <Card
      role="alert"
      className={
        tone === "error"
          ? "border-destructive/40 bg-destructive/10"
          : "border-status-warn/40 bg-status-warn-soft"
      }
    >
      <CardContent className="flex items-start gap-2.5 text-sm">
        <AlertTriangle
          className={`mt-0.5 size-4 shrink-0 ${tone === "error" ? "text-destructive" : "text-status-warn"}`}
          aria-hidden="true"
        />
        <span>{message}</span>
      </CardContent>
    </Card>
  );

  if (!threadId) {
    return (
      <div className="space-y-4">
        {backLink}
        {alert("This link is missing a run id. Pick a screening from the review queue.", "warn")}
      </div>
    );
  }

  if (!state) {
    return (
      <div className="space-y-4">
        {backLink}
        {error && alert(error)}
        {/* Shaped like the page it precedes (#49) — header, then the criteria
            buckets — so the run lands in place instead of pushing the page
            about as it arrives. */}
        {!error && <CriteriaEditorSkeleton />}
      </div>
    );
  }

  const record = state.screening;
  const phaseLabel =
    state.pending.length > 0
      ? "awaiting_approval"
      : (record?.status ?? state.values.current_step ?? "");
  const filename = record?.source_filename ?? state.values.source_filename ?? "Screening";
  const revision = state.values.criteria_revision ?? 0;
  const edits = state.values.criteria_edits ?? [];
  const findings = state.values.compliance_findings ?? [];
  const blocking = findings.filter((f) => f.severity === "reject");
  const busy = phase === "rerunning";
  // Already stopped at the gate (#91). Read from the durable trail rather than
  // from the status, so a rejected run says who and why even if the store row is
  // behind. It is terminal — the API refuses further edits — so the actions below
  // are replaced by the decision rather than left to 409.
  const rejectedBy = state.values.rejected_by;
  // Where rejecting is still a legal decision, mirroring the API's own rule:
  // parked at the approval gate, or escalated after the Critic gave up.
  const rejectable =
    !rejectedBy && (state.pending.includes("matcher") || state.values.current_step === "escalated");

  /**
   * The "this was mis-parsed" exit, rendered beside Delete on every typed
   * criterion (#92). Shared by both form shapes so the two rows offer the same
   * action in the same place — a reviewer scanning a page of criteria should not
   * have to learn where the button moved to.
   */
  const demoteButton = (
    bucket: QuantBucket | CatBucket,
    index: number,
    criterion: QuantitativeCriterion | CategoricalCriterion
  ) => {
    const sentence = demotionText(criterion);
    return (
      <Button
        variant="ghost"
        size="icon"
        aria-label="Send this criterion back to unparseable"
        title={
          sentence
            ? "The Parser got this one wrong — put its sentence back in Unparseable"
            : "No source sentence recorded, so there is nothing to send back."
        }
        disabled={busy || sentence === null}
        onClick={() => demote(bucket, index)}
      >
        <CornerUpLeft aria-hidden="true" />
      </Button>
    );
  };

  return (
    <div className="space-y-4" data-region="criteria-editor" data-phase={phaseLabel}>
      {backLink}

      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            {filename}
            {phaseLabel && (
              <Badge variant={statusVariant(phaseLabel)}>{statusLabel(phaseLabel)}</Badge>
            )}
            <Badge variant="secondary">Revision {revision}</Badge>
          </CardTitle>
          <p className="text-muted-foreground font-mono text-xs break-all">
            {threadId}
            {record ? ` · uploaded ${formatTimestamp(record.created_at)}` : ""}
          </p>
        </CardHeader>
      </Card>

      {error && alert(error)}

      {/* The decision, if it has already been made (#91). Above the criteria, not
          below them: a reader who opens a rejected run needs to know it was
          stopped — and on whose word — before they start reading an extraction
          nobody will ever score patients against. */}
      {rejectedBy && (
        <Card
          className="border-destructive/40 bg-destructive/10"
          role="status"
          data-region="rejection-provenance"
        >
          <CardContent className="flex items-start gap-2.5 text-sm">
            <Ban className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span className="font-medium">Rejected by {rejectedBy}</span>
              {state.values.rejected_by_role ? ` (${state.values.rejected_by_role})` : ""}
              {state.values.rejected_at ? ` on ${formatTimestamp(state.values.rejected_at)}` : ""}.
              No patient data was matched, and these criteria can no longer be edited.
              {state.values.rejected_reason && (
                <span className="block pt-1">{state.values.rejected_reason}</span>
              )}
            </span>
          </CardContent>
        </Card>
      )}

      {outcome && (
        <Card
          role="status"
          data-region="rerun-outcome"
          data-outcome={outcome.ok ? "passed" : "blocked"}
          className={
            outcome.ok
              ? "border-primary/40 bg-primary/10"
              : "border-status-warn/40 bg-status-warn-soft"
          }
        >
          <CardContent className="flex flex-wrap items-center gap-2.5 text-sm">
            {outcome.ok ? (
              <CheckCircle2 className="text-primary size-4 shrink-0" aria-hidden="true" />
            ) : (
              <AlertTriangle className="text-status-warn size-4 shrink-0" aria-hidden="true" />
            )}
            <span className="flex-1">{outcome.text}</span>
            <Link
              href={runHref(threadId)}
              className="text-primary text-sm underline-offset-4 hover:underline"
            >
              Open the run
            </Link>
          </CardContent>
        </Card>
      )}

      {/* Why this run is here. The Critic's blocking findings are the reviewer's
          work list, so they belong above the fields, not buried under them —
          in plain language by default (#52), since fixing them is the job of
          whoever knows the protocol, not whoever knows the rule engine. */}
      <ComplianceFindings
        findings={blocking}
        summary={state.values.compliance_summary}
        title="What the Critic rejected"
        region="blocking-findings"
        className="border-status-warn/40 bg-status-warn-soft"
        // This page holds an unsaved draft with no navigation guard, so reading
        // up on the rule that blocked the run opens beside the corrections
        // rather than throwing them away (#57).
        ruleLinksInNewTab
      />

      {!draft ? (
        alert(
          "This screening has no extracted criteria to edit — its run never got past the parser.",
          "warn"
        )
      ) : (
        <>
          {QUANT_BUCKETS.map((bucket) => (
            <Card key={bucket} data-region={bucket}>
              <CardHeader>
                <CardTitle className="text-base">{SECTION_TITLES[bucket]}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {draft[bucket].length === 0 && (
                  <p className="text-muted-foreground text-sm">None extracted.</p>
                )}
                {draft[bucket].map((criterion, index) => (
                  <div
                    key={index}
                    className="border-border grid gap-2 border-t pt-3 first:border-t-0 first:pt-0 lg:grid-cols-2"
                    data-criterion={index}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <select
                        className={`${FIELD} min-w-36`}
                        aria-label="Attribute"
                        value={criterion.attribute}
                        disabled={busy}
                        onChange={(e) => patchQuant(bucket, index, { attribute: e.target.value })}
                      >
                        {/* An attribute the parser produced that isn't in the
                            closed list would otherwise vanish from the select and
                            be silently rewritten to the first option on save. */}
                        {!EHR_ATTRIBUTES.includes(
                          criterion.attribute as (typeof EHR_ATTRIBUTES)[number]
                        ) && <option value={criterion.attribute}>{criterion.attribute}</option>}
                        {EHR_ATTRIBUTES.map((attribute) => (
                          <option key={attribute} value={attribute}>
                            {attribute}
                          </option>
                        ))}
                      </select>
                      <select
                        className={`${FIELD} w-24`}
                        aria-label="Operator"
                        value={criterion.operator}
                        disabled={busy}
                        onChange={(e) =>
                          patchQuant(bucket, index, {
                            operator: e.target.value as QuantitativeCriterion["operator"],
                            // Leaving a stale upper bound behind would submit a
                            // value the API ignores and the diff still reports.
                            ...(e.target.value === "between" ? {} : { value_high: null }),
                          })
                        }
                      >
                        {OPERATORS.map((operator) => (
                          <option key={operator} value={operator}>
                            {operator}
                          </option>
                        ))}
                      </select>
                      <input
                        type="number"
                        step="any"
                        className={`${FIELD} w-24`}
                        aria-label="Value"
                        value={fromNumber(criterion.value)}
                        disabled={busy}
                        onChange={(e) =>
                          patchQuant(bucket, index, { value: toNumber(e.target.value) })
                        }
                      />
                      {criterion.operator === "between" && (
                        <input
                          type="number"
                          step="any"
                          className={`${FIELD} w-24`}
                          aria-label="Upper bound"
                          value={fromNumber(criterion.value_high)}
                          disabled={busy}
                          onChange={(e) =>
                            patchQuant(bucket, index, { value_high: toNumber(e.target.value) })
                          }
                        />
                      )}
                      <input
                        className={`${FIELD} w-32`}
                        aria-label="Unit"
                        placeholder="unit"
                        value={criterion.unit}
                        disabled={busy}
                        onChange={(e) => patchQuant(bucket, index, { unit: e.target.value })}
                      />
                      {demoteButton(bucket, index, criterion)}
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Delete this criterion"
                        disabled={busy}
                        onClick={() => removeAt(bucket, index)}
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </div>
                    {/* Provenance, read-only. It is the protocol's words, and the
                        thing that makes an edit auditable — a reviewer who could
                        rewrite it could make any threshold look justified. */}
                    <blockquote className="text-muted-foreground border-border border-l-2 pl-3 text-sm">
                      {criterion.source_text || "No source sentence recorded."}
                    </blockquote>
                  </div>
                ))}
              </CardContent>
            </Card>
          ))}

          {CAT_BUCKETS.map((bucket) => (
            <Card key={bucket} data-region={bucket}>
              <CardHeader>
                <CardTitle className="text-base">{SECTION_TITLES[bucket]}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {draft[bucket].length === 0 && (
                  <p className="text-muted-foreground text-sm">None extracted.</p>
                )}
                {draft[bucket].map((criterion, index) => (
                  <div
                    key={index}
                    className="border-border grid gap-2 border-t pt-3 first:border-t-0 first:pt-0 lg:grid-cols-2"
                    data-criterion={index}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <select
                        className={`${FIELD} min-w-36`}
                        aria-label="Category"
                        value={criterion.category}
                        disabled={busy}
                        onChange={(e) =>
                          patchCat(bucket, index, {
                            category: e.target.value as CategoricalCriterion["category"],
                          })
                        }
                      >
                        {CATEGORIES.map((category) => (
                          <option key={category} value={category}>
                            {category.replace("_", " ")}
                          </option>
                        ))}
                      </select>
                      <input
                        className={`${FIELD} min-w-48 flex-1`}
                        aria-label="Term"
                        placeholder="normalized term"
                        value={criterion.value}
                        disabled={busy}
                        onChange={(e) => patchCat(bucket, index, { value: e.target.value })}
                      />
                      {/* Only meaningful on the inclusion side: the exclusion list
                          already means "must not have this" (see the schema). */}
                      {bucket === "inclusion_categorical" && (
                        <label className="flex items-center gap-1.5 text-sm">
                          <input
                            type="checkbox"
                            className="accent-primary size-4"
                            checked={criterion.negated}
                            disabled={busy}
                            onChange={(e) => patchCat(bucket, index, { negated: e.target.checked })}
                          />
                          must NOT have
                        </label>
                      )}
                      {demoteButton(bucket, index, criterion)}
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Delete this criterion"
                        disabled={busy}
                        onClick={() => removeAt(bucket, index)}
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </div>
                    <blockquote className="text-muted-foreground border-border border-l-2 pl-3 text-sm">
                      {criterion.source_text || "No source sentence recorded."}
                    </blockquote>
                  </div>
                ))}
              </CardContent>
            </Card>
          ))}

          <Card data-region="unparseable">
            <CardHeader>
              <CardTitle className="text-base">Unparseable</CardTitle>
              <p className="text-muted-foreground text-sm">
                Sentences the Parser refused to turn into criteria rather than invent a number for,
                plus any you sent back down from the sections above. Reclassify one into a real
                criterion, or delete it if it isn&apos;t eligibility criteria at all — a sentence
                left here is recorded as unscreenable rather than scored.
              </p>
            </CardHeader>
            <CardContent className="space-y-3">
              {draft.unparseable.length === 0 && (
                <p className="text-muted-foreground text-sm">Nothing left unparsed.</p>
              )}
              {draft.unparseable.map((sentence, index) => (
                <div
                  key={index}
                  className="border-border space-y-2 border-t pt-3 first:border-t-0 first:pt-0"
                  data-criterion={index}
                >
                  <p className="text-sm">{sentence}</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="text-muted-foreground text-xs" htmlFor={`reclass-${index}`}>
                      Reclassify as
                    </label>
                    <select
                      id={`reclass-${index}`}
                      className={`${FIELD} min-w-56`}
                      // Stays on the placeholder: picking an option performs the
                      // move, after which this row no longer exists.
                      value=""
                      disabled={busy}
                      onChange={(e) =>
                        e.target.value &&
                        reclassify(index, e.target.value as QuantBucket | CatBucket)
                      }
                    >
                      <option value="">choose a bucket…</option>
                      {[...QUANT_BUCKETS, ...CAT_BUCKETS].map((bucket) => (
                        <option key={bucket} value={bucket}>
                          {SECTION_TITLES[bucket]}
                        </option>
                      ))}
                    </select>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Delete this sentence"
                      disabled={busy}
                      onClick={() => removeAt("unparseable", index)}
                    >
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>

          {/* Both exits are gone once the run has been rejected: the API refuses
              an edit to a rejected run, and offering a button that can only 409 is
              worse than saying the decision is final (which the card above does). */}
          {!rejectedBy && (
            <Card className="border-primary/40 bg-primary/10" data-region="rerun-actions">
              <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:items-center">
                <span className="flex-1">
                  Re-running sends these criteria back through the compliance Critic. It does not
                  touch patient data — the run returns to the approval gate for a named approval
                  first.
                </span>
                <Button
                  variant="outline"
                  className="shrink-0"
                  disabled={busy}
                  onClick={() =>
                    setDraft(
                      state.values.parsed_criteria
                        ? structuredClone(state.values.parsed_criteria)
                        : null
                    )
                  }
                >
                  <RotateCcw aria-hidden="true" />
                  Discard my changes
                </Button>
                <Button size="lg" className="shrink-0" disabled={busy} onClick={rerun}>
                  <Play aria-hidden="true" />
                  {busy ? "Re-running…" : "Save & re-run"}
                </Button>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {/* The third exit (#91), and the only one that is not about fixing the
          extraction: some protocols cannot be screened however the criteria are
          worded. Its own card, below the re-run actions, because it is the answer
          a reviewer reaches only after deciding the others won't do — and it is
          offered whether or not there is an extraction to edit, since a run with
          nothing parseable in it is exactly one worth stopping. */}
      {rejectable && (
        <Card data-region="reject-actions">
          <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:flex-wrap sm:items-center">
            <span className="flex-1">
              If this protocol cannot be screened at all — the wrong document, an eligibility
              section that isn&apos;t one, criteria this cohort has no data for — stop the run here
              rather than leaving it parked. Your name and reason are recorded against it.
            </span>
            <RejectScreening
              threadId={threadId}
              disabled={busy}
              // Re-read the checkpoint rather than patching local state: the
              // rejection banner, the phase badge and the timeline all render from
              // the server's copy, and one fetch keeps them from disagreeing.
              onRejected={() => setReloadToken((token) => token + 1)}
            />
          </CardContent>
        </Card>
      )}

      {/* The before/after the issue asks for, read back from the checkpoint after
          the re-run rather than diffed locally — the server's record is the one
          that survives this page being closed. */}
      <CriteriaDiff edits={edits} />
    </div>
  );
}
