"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, BarChart3, Coins, Gauge, Layers, ShieldCheck, Timer } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MetricsSkeleton } from "@/components/skeletons";
import { apiFetch, problemDetail } from "@/lib/api";
import {
  formatCount,
  formatDuration,
  formatShare,
  formatTokens,
  formatUsd,
  outcomeBarClass,
} from "@/lib/metrics";
import { ruleHref } from "@/lib/rules";
import { formatTimestamp } from "@/lib/runs";
import { cn } from "@/lib/utils";
import type {
  CostSummary,
  CoverageAggregate,
  MetricsSummary,
  NodeLatency,
  TermMappingCache,
} from "@/types";

/**
 * The in-app metrics summary (#58).
 *
 * The backend has exported domain metrics since #7, but reading a screening
 * funnel out of `screenings_total{outcome="escalated"} 3.0` needs a Prometheus and
 * a dashboard in front of it. This page answers the three questions a reviewer
 * actually asks of the pipeline — where do runs end up, which rules block
 * protocols, and how often does the Parser get it right first time — and stops
 * there. Grafana still owns time series, percentiles and alerting; this
 * complements it rather than competing with it, which is why there is not a chart
 * axis in sight.
 *
 * Every number comes from `GET /api/metrics/summary`, which reads the same
 * collectors `/metrics` serializes (backend/app/services/metrics_summary.py) — so
 * this page and a scrape are one source read twice, and the shares and bucket
 * captions arrive already resolved rather than being re-derived here.
 *
 * The counters are process-scoped and reset when the instance restarts, so the
 * footer states the window they cover. That is the one way a page like this could
 * mislead: "9 completed" means something very different on a process up for a week.
 */
export function MetricsOverview() {
  const [summary, setSummary] = useState<MetricsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiFetch("/api/metrics/summary")
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          setError(await problemDetail(response, "Could not load the metrics summary"));
          return;
        }
        setSummary((await response.json()) as MetricsSummary);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return (
      <Card className="border-destructive/40 bg-destructive/10" role="alert">
        <CardContent className="flex items-start gap-2.5 text-sm">
          <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </CardContent>
      </Card>
    );
  }

  if (!summary) return <MetricsSkeleton />;

  const { funnel, rejections, attempts, coverage, cost, latency, term_mapping } = summary;
  // Every counter, not just the funnel: a run that the Critic has already pushed
  // back but that has not yet reached a terminal outcome bumps the rejection
  // counter while the funnel is still empty. Gating on the funnel alone would
  // hide that finding behind a "nothing has run yet" notice. Tokens count too:
  // a run still mid-parse has already spent money the page can report (#101).
  const nothingCounted =
    funnel.total === 0 && rejections.total === 0 && attempts.observations === 0 && !cost?.tokens;

  return (
    <div className="space-y-4" data-region="metrics-overview">
      {/* Above the counter panels, and outside the cold-start branch below: this
          one is derived from the durable checkpoints rather than from this
          process's registry, so it has data on an instance that restarted a minute
          ago — exactly when the three panels below are empty. */}
      {coverage && coverage.runs > 0 && <Screenability coverage={coverage} />}
      {nothingCounted ? (
        <ColdStart since={summary.since} />
      ) : (
        // Funnel and depth are three-to-four rows each and pair naturally; the
        // rejection breakdown is one row per rule the instance has ever blocked
        // on, so it takes the full width below rather than squeezing rule ids and
        // bars into half of it.
        <div className="grid items-start gap-3 lg:grid-cols-2">
          <Funnel funnel={funnel} />
          <Attempts attempts={attempts} />
          <Rejections rejections={rejections} runs={funnel.total} />
          {/* Cost and latency (#101) below the pipeline panels: they describe
              what the pipeline above *costs*, and a reader wants to know where
              runs end up before asking what they spent getting there. */}
          {cost && cost.tokens > 0 && <Cost cost={cost} />}
          {latency && latency.length > 0 && <Latency latency={latency} />}
          {term_mapping && term_mapping.resolutions > 0 && <TermMapping cache={term_mapping} />}
        </div>
      )}
      <Provenance
        since={summary.since}
        exported={summary.exported}
        estimatedPercentiles={summary.estimated_percentiles ?? false}
      />
    </div>
  );
}

/**
 * One row: what it is, how many, what share of the panel, and a bar.
 *
 * The bar is `aria-hidden` — it is a second encoding of the figure beside it, not
 * information of its own, so a screen reader reads "Completed, 9 runs, 75%" and
 * never announces an empty div.
 */
function MeterRow({
  label,
  count,
  share,
  barClass,
  noun,
  plural,
}: {
  label: React.ReactNode;
  count: number;
  share: number;
  barClass: string;
  noun: string;
  /** For a noun an "s" does not pluralize — "criterion" (#93). */
  plural?: string;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="min-w-0 flex-1 text-sm">{label}</span>
        {/* The bare figure carries the column's alignment; the noun that makes it
            a sentence is read only by assistive tech, which has no column to
            infer it from. */}
        <span className="text-sm font-medium tabular-nums" aria-hidden="true">
          {count}
        </span>
        <span className="sr-only">{formatCount(count, noun, plural)}</span>
        <span className="text-muted-foreground w-14 text-right text-xs tabular-nums">
          {formatShare(share)}
        </span>
      </div>
      <div className="bg-muted h-1.5 overflow-hidden rounded-full" aria-hidden="true">
        {/* Width from the same `share` the figure prints, so the bar can never
            disagree with the number beside it. A populated row keeps a hairline
            of colour (min-w) — a bar that rounds to invisible reads as zero. */}
        <div
          className={cn("h-full rounded-full", count > 0 && "min-w-0.5", barClass)}
          style={{ width: `${share}%` }}
        />
      </div>
    </div>
  );
}

/** The shared card frame: an icon, a title, and one line of what the panel means. */
function Panel({
  icon: Icon,
  title,
  caption,
  region,
  className,
  children,
}: {
  icon: LucideIcon;
  title: string;
  caption: string;
  region: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Card data-region={region} className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon className="text-muted-foreground size-4" aria-hidden="true" />
          {title}
        </CardTitle>
        <p className="text-muted-foreground text-xs">{caption}</p>
      </CardHeader>
      <CardContent className="space-y-3">{children}</CardContent>
    </Card>
  );
}

/**
 * Where runs end up. Every terminal outcome is listed even at zero — unlike the
 * panels below, the *absence* of failures is the fact worth showing, and a funnel
 * that changed shape as outcomes appeared would be unreadable at a glance.
 */
function Funnel({ funnel }: { funnel: MetricsSummary["funnel"] }) {
  return (
    <Panel
      icon={BarChart3}
      title="Screening funnel"
      caption={`${formatCount(funnel.total, "run")} reached a terminal outcome.`}
      region="metrics-funnel"
    >
      {funnel.outcomes.map((outcome) => (
        <MeterRow
          key={outcome.outcome}
          label={outcome.label}
          count={outcome.count}
          share={outcome.share}
          barClass={outcomeBarClass(outcome.outcome)}
          noun="run"
        />
      ))}
    </Panel>
  );
}

/**
 * Which rules protocols fall foul of, ranked.
 *
 * Each rule id links into the rules database (#57), so "RENAL-001 blocks more
 * protocols than anything else" is one click from what RENAL-001 actually
 * requires — the same link every Critic finding carries.
 *
 * The count is of blocking *findings*, not of rejected runs: the Critic can trip
 * two rules in one pass and can send the same protocol back three times. The
 * caption says so, because "10 rejections across 12 runs" invites exactly the
 * wrong reading otherwise.
 */
function Rejections({
  rejections,
  runs,
}: {
  rejections: MetricsSummary["rejections"];
  runs: number;
}) {
  return (
    <Panel
      icon={ShieldCheck}
      title="Critic rejections by rule"
      caption={
        rejections.total === 0
          ? // Nothing to rank yet, so the caption defines what the panel counts
            // rather than repeating the good news the body below already carries.
            "Blocking findings, counted once per rule per Critic pass."
          : runs === 0
            ? // A run the Critic has pushed back but that has not yet finished:
              // there are findings and no completed runs to divide them by, so the
              // rate is left out rather than printed as "0 per run".
              `${formatCount(rejections.total, "blocking finding")} so far, on a run that has not` +
              " finished. One pass can trip several rules."
            : `${formatCount(rejections.total, "blocking finding")} across ${formatCount(
                runs,
                "run"
              )} — ${rejections.per_run} per run. One pass can trip several rules.`
      }
      region="metrics-rejections"
      className="lg:col-span-2"
    >
      {rejections.rules.length === 0 ? (
        <p className="text-muted-foreground text-sm" data-region="metrics-rejections-empty">
          Every extraction has cleared the compliance rules so far.
        </p>
      ) : (
        rejections.rules.map((rule) => (
          <MeterRow
            key={rule.rule_id}
            label={
              <span className="flex flex-wrap items-center gap-1.5">
                <Link
                  href={ruleHref(rule.rule_id)}
                  title={`What ${rule.rule_id} checks`}
                  className="hover:text-primary font-mono text-xs underline-offset-4 hover:underline"
                >
                  {rule.rule_id}
                </Link>
                {rule.layer === "semantic" && (
                  // The LLM layer has no threshold in the rules file. Saying so
                  // keeps a reviewer from reading the top row as a fixed rule.
                  <Badge variant="outline">Model review</Badge>
                )}
              </span>
            }
            count={rule.count}
            share={rule.share}
            barClass="bg-primary"
            noun="finding"
          />
        ))
      )}
    </Panel>
  );
}

/**
 * How deep the parse/critic loop ran — the accuracy figure, read from the other
 * end: a first-pass share of 90% means the Parser rarely needed correcting.
 *
 * Counted only for runs whose loop resolved, so this total is smaller than the
 * funnel's whenever a run failed outright (an aborted run's attempt count would
 * describe an outage, not loop depth). The caption states the population rather
 * than leaving a reader to wonder why two panels disagree.
 */
function Attempts({ attempts }: { attempts: MetricsSummary["attempts"] }) {
  const firstPass =
    attempts.first_pass_share === null
      ? ""
      : ` ${formatShare(attempts.first_pass_share)} needed only one.`;
  return (
    <Panel
      icon={Layers}
      title="Parser attempts per run"
      caption={
        attempts.observations === 0
          ? "No run has completed the parse/critic loop yet."
          : `${formatCount(attempts.mean, "attempt")} on average across ${formatCount(
              attempts.observations,
              "resolved run"
            )}.${firstPass}`
      }
      region="metrics-attempts"
    >
      {attempts.buckets.length === 0 ? (
        <p className="text-muted-foreground text-sm" data-region="metrics-attempts-empty">
          Depth is recorded once a run&apos;s extraction is accepted or escalated.
        </p>
      ) : (
        attempts.buckets.map((bucket) => (
          <MeterRow
            key={bucket.label}
            label={
              <>
                {bucket.label}
                <span className="text-muted-foreground">
                  {" "}
                  {bucket.label === "1" ? "try" : "tries"}
                </span>
              </>
            }
            count={bucket.count}
            share={bucket.share}
            barClass="bg-primary"
            noun="run"
          />
        ))
      )}
    </Panel>
  );
}

/**
 * What the models cost (#101) — the panel that turns the project's central
 * architectural claim into a measurement.
 *
 * The claim is that a model is used only where language understanding is
 * genuinely required. The median cost of one screening is what makes it
 * checkable: a hundred-patient cohort screened for cents is an argument, and one
 * screened for dollars is a bug report. The split by agent is where that argument
 * gets specific — if the Matcher's share ever rivals the Parser's, the term
 * mapping stopped being cached.
 *
 * The median is estimated from histogram buckets rather than kept exactly (the
 * footer says so), and a deployment whose models have no configured price shows
 * real tokens with no dollars at all — because "we are not billed for this" is a
 * different statement from "this was free", and only one of them is true of a
 * local model.
 */
function Cost({ cost }: { cost: CostSummary }) {
  return (
    <Panel
      icon={Coins}
      title="Cost per screening"
      caption={
        cost.calls_priced
          ? `${formatUsd(cost.median_cost_usd)} median across ${formatCount(
              cost.screenings,
              "finished run"
            )}, ${formatUsd(cost.mean_cost_usd)} on average — ${formatUsd(
              cost.total_cost_usd
            )} spent in total.`
          : `${formatTokens(cost.tokens)} tokens across ${formatCount(
              cost.screenings,
              "finished run"
            )}. No model here has a configured price, so nothing is billed.`
      }
      region="metrics-cost"
    >
      {cost.nodes.map((node) => (
        <div className="space-y-1" key={node.node} data-region="metrics-cost-node">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="min-w-0 flex-1 text-sm capitalize">
              {node.node}
              <span className="text-muted-foreground normal-case">
                {" "}
                · {formatTokens(node.tokens)} tokens
              </span>
            </span>
            <span className="text-sm font-medium tabular-nums">
              {cost.calls_priced ? formatUsd(node.median_cost_usd) : "—"}
            </span>
            <span className="text-muted-foreground w-20 text-right text-xs tabular-nums">
              {cost.calls_priced ? `${formatUsd(node.total_cost_usd)} total` : ""}
            </span>
          </div>
        </div>
      ))}
      <p className="text-muted-foreground text-sm">
        {cost.calls_priced
          ? `Median per screening by agent, with the instance total beside it. A run's 95th percentile is ${formatUsd(
              cost.p95_cost_usd
            )}.`
          : "Token counts are real; the price table (LLM_PRICES) has no entry for the models this instance runs."}
      </p>
    </Panel>
  );
}

/**
 * How long each agent takes, at the middle and at the tail (#101).
 *
 * `agent_node_duration_seconds` has carried this since #7, but reading it meant
 * standing up a Prometheus — so the one figure an operator asks for most often
 * ("which agent is slow, and how bad does it get?") was the one this page
 * couldn't answer. p95 next to p50 rather than instead of it: the gap between
 * them is the question, since an agent whose tail is ten times its median is a
 * different problem from one that is uniformly slow.
 */
function Latency({ latency }: { latency: NodeLatency[] }) {
  return (
    <Panel
      icon={Timer}
      title="Agent latency"
      caption="Wall-clock per node execution, at the median and the 95th percentile."
      region="metrics-latency"
    >
      {latency.map((node) => (
        <div
          className="flex flex-wrap items-baseline gap-x-2"
          key={node.node}
          data-region="metrics-latency-node"
        >
          <span className="min-w-0 flex-1 text-sm capitalize">
            {node.node}
            <span className="text-muted-foreground normal-case">
              {" "}
              · {formatCount(node.runs, "run")}
            </span>
          </span>
          <span className="text-sm font-medium tabular-nums">
            {formatDuration(node.p50_seconds)}
          </span>
          <span className="text-muted-foreground w-20 text-right text-xs tabular-nums">
            {formatDuration(node.p95_seconds)} p95
          </span>
        </div>
      ))}
    </Panel>
  );
}

/**
 * What the Matcher's term-mapping cache saved (#101).
 *
 * The Matcher resolves each ambiguous `(criterion, term)` pair once per
 * *screening* and reuses the verdict across every patient in the cohort. This
 * panel is that design decision as a number: the questions the cohort's records
 * raised, against the ones a model was actually asked. The gap is the whole
 * argument for the caching, and it widens with cohort size — which is exactly why
 * a flat figure would be the wrong thing to show.
 */
function TermMapping({ cache }: { cache: TermMappingCache }) {
  return (
    <Panel
      icon={Layers}
      title="Term-mapping cache"
      caption="Criterion/term questions the cohorts raised, against the ones that reached a model."
      region="metrics-term-mapping"
      // Full width, like the rejection breakdown: one meter row and a sentence
      // squeezed into half a grid leaves an empty column beside it, and the
      // sentence is the point — the ratio only means something once you know
      // what the two numbers in it are.
      className="lg:col-span-2"
    >
      <MeterRow
        label={
          <>
            Served from cache
            <span className="text-muted-foreground"> instead of the model</span>
          </>
        }
        count={cache.served_from_cache}
        share={cache.hit_rate ?? 0}
        barClass="bg-status-pass"
        noun="resolution"
      />
      <p className="text-muted-foreground text-sm">
        {formatCount(cache.resolutions, "resolution")} needed across every cohort screened;{" "}
        {formatCount(cache.llm_pairs, "distinct pair")} went to a model. Mappings are resolved once
        per screening, so this rate rises with cohort size rather than staying flat.
      </p>
    </Panel>
  );
}

/**
 * Screenability across recent runs (#93) — and the phrasings that cost the most
 * of it, which is the point of aggregating it at all.
 *
 * The per-run score tells a reviewer what one protocol could be screened on. Pooled
 * over runs it answers a different question, and one nobody could answer before: *which
 * wording should the `EhrAttribute` vocabulary swallow next*. A phrasing that shows
 * up in nine of fifty protocols is a week of parser work that pays for itself; one
 * that shows up once is a protocol quirk. That ranking is the backlog, and it is
 * data rather than a hunch.
 *
 * The window is stated twice on purpose — in the caption and in the footer of the
 * list — because this panel is a sample where the three beside it are totals, and a
 * ranking that read as all-time would send someone off to implement the wrong
 * attribute.
 */
function Screenability({ coverage }: { coverage: CoverageAggregate }) {
  const uncheckable = coverage.criteria - coverage.checkable;
  return (
    <Panel
      icon={Gauge}
      title="Protocol coverage"
      caption={
        `${coverage.checkable} of ${coverage.criteria} criteria across ` +
        `${formatCount(coverage.runs, "run")} could be checked` +
        (coverage.sampled < coverage.total
          ? ` — the ${coverage.sampled} most recent of ${coverage.total}.`
          : ".")
      }
      region="metrics-coverage"
    >
      <MeterRow
        label={
          <>
            Checkable
            <span className="text-muted-foreground"> criteria</span>
          </>
        }
        count={coverage.checkable}
        share={coverage.score}
        barClass="bg-status-pass"
        noun="criterion"
        plural="criteria"
      />
      <p className="text-muted-foreground text-sm">
        {uncheckable === 0 ? (
          // Conditioned on how many of these runs actually reached the Matcher: on
          // a window of runs parked at the gate, "and evaluated" would report a
          // check none of them ran.
          coverage.scored === coverage.runs ? (
            "Every criterion these runs extracted was structured and evaluated."
          ) : (
            "Every criterion these runs extracted was structured; " +
            `${coverage.runs - coverage.scored} of them have not been matched yet.`
          )
        ) : (
          <>
            {formatCount(coverage.unparseable, "sentence")} never became a criterion and{" "}
            {formatCount(coverage.unresolved, "criterion", "criteria")} could not be evaluated
            against patient records
            {coverage.scored < coverage.runs
              ? ` (${coverage.runs - coverage.scored} of these runs never reached matching).`
              : "."}
          </>
        )}
      </p>
      {coverage.phrases.length > 0 && (
        <div className="border-t pt-3" data-region="metrics-coverage-phrases">
          <h3 className="text-muted-foreground mb-1.5 text-xs font-medium">
            Phrasings the vocabulary cannot parse
          </h3>
          <div className="space-y-3">
            {coverage.phrases.map((phrase) => (
              <MeterRow
                key={phrase.text}
                label={
                  <span className="block">
                    {phrase.text}
                    <span className="text-muted-foreground block text-xs">
                      in {formatCount(phrase.runs, "run")}
                    </span>
                  </span>
                }
                count={phrase.count}
                share={phrase.share}
                barClass="bg-status-warn"
                noun="sentence"
              />
            ))}
          </div>
          {coverage.phrasings > coverage.phrases.length && (
            // No silent truncation: the ranking is capped, and a reader deciding
            // what to build next has to know how much tail sits below it.
            <p className="text-muted-foreground pt-2 text-xs">
              The {coverage.phrases.length} most frequent of {coverage.phrasings} distinct phrasings
              in this window.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}

/**
 * A fresh instance, which is the *usual* state here rather than an edge case: the
 * counters live in the serving process's memory, so every restart empties them.
 * Three empty panels would look like a broken page; saying why is the whole
 * content of this state.
 */
function ColdStart({ since }: { since: string }) {
  return (
    <Card data-region="metrics-empty">
      <CardContent className="text-muted-foreground flex flex-col items-center gap-2 py-8 text-center text-sm">
        <BarChart3 className="size-5" aria-hidden="true" />
        <span>No screening has reached a terminal outcome since this instance started.</span>
        <span className="text-xs">
          Counting began {formatTimestamp(since)}. Run a screening and the funnel fills in.
        </span>
      </CardContent>
    </Card>
  );
}

/**
 * Where the numbers came from and what window they cover — the footer that makes
 * the page auditable rather than merely informative.
 */
function Provenance({
  since,
  exported,
  estimatedPercentiles,
}: {
  since: string;
  exported: boolean;
  estimatedPercentiles: boolean;
}) {
  return (
    <p className="text-muted-foreground text-sm" data-region="metrics-provenance">
      Counted in this instance since {formatTimestamp(since)}; the counters live in the serving
      process and reset when it restarts.{" "}
      {exported
        ? "They are the same custom metrics /metrics exposes to Prometheus — Grafana has the history and trends this page deliberately leaves out."
        : "Prometheus export is switched off on this instance (METRICS_ENABLED), so these counters are recorded but nothing is scraping them."}
      {/* Said plainly rather than left to be assumed: a median read off bucket
          boundaries is close, not exact, and a page that presented it as exact
          would be making a claim about the API that isn't true. */}
      {estimatedPercentiles
        ? " Medians and 95th percentiles are estimated from histogram buckets rather than from every observation."
        : ""}
    </p>
  );
}
