"use client";

import { Coins } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCount, formatTokens, formatUsd } from "@/lib/metrics";
import { cn } from "@/lib/utils";
import type { RunNodeUsage, RunUsage } from "@/types";

/**
 * What this run's LLM calls consumed and cost (#101).
 *
 * The project's central claim is that it uses a model only where language
 * understanding is actually required — deterministic comparison everywhere else,
 * and term mappings resolved once per screening rather than once per patient.
 * Every other panel on this page shows what the pipeline *decided*; this one
 * shows what those decisions cost, which is the only way that claim stops being
 * an assertion. A four-call, two-cent screening of a hundred patients says more
 * about the architecture than any amount of prose.
 *
 * Every figure is derived server-side (`backend/app/services/usage.py`) from
 * `values.llm_usage`, the same per-call record the graph appends to as it runs,
 * so this panel renders numbers rather than computing them — down to the dollars,
 * which come out of the API's one micro-USD conversion.
 *
 * Three editorial decisions. Calls lead, not dollars: on a local deployment the
 * dollars are all zero and the call count is the whole story. A run whose models
 * carry no price says "no cost" rather than "$0.00", because "we are not billed
 * for this" and "this was free" are different claims. And an estimated token
 * count is labelled as one — the load-test stub does no inference and reports
 * nothing, so its tokens are inferred from characters, and a figure that hid that
 * would be the one way this panel could mislead.
 */
export function UsagePanel({ usage }: { usage?: RunUsage }) {
  // A run that never reached the Parser made no call, so there is no bill to
  // show — the page renders nothing rather than a row of zeros. An older payload
  // with no `usage` key lands here too.
  if (!usage || usage.calls === 0) return null;

  const estimated = usage.estimated_calls > 0;

  return (
    <Card data-region="usage" data-calls={usage.calls}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Coins className="text-muted-foreground size-4" aria-hidden="true" />
          What this run cost
        </CardTitle>
        <p className="text-muted-foreground text-xs">
          Tokens and estimated spend across every model call this screening made, by agent.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-3" data-region="usage-totals">
          <Figure label="LLM calls" value={String(usage.calls)} />
          <Figure label="Tokens" value={formatTokens(usage.tokens)} />
          <Figure
            label="Estimated cost"
            // "No cost" is the honest reading for a local or stubbed model: the
            // tokens are real, the invoice is not. Printing "$0.00" would claim a
            // priced call that came to nothing.
            value={usage.priced ? formatUsd(usage.cost_usd) : "No cost"}
          />
        </div>

        <div className="space-y-3 border-t pt-3" data-region="usage-nodes">
          {usage.nodes.map((node) => (
            <NodeRow key={node.node} node={node} total={usage.tokens} priced={usage.priced} />
          ))}
        </div>

        <p className="text-muted-foreground text-xs">
          {usage.priced
            ? `${formatTokens(usage.prompt_tokens)} prompt and ${formatTokens(
                usage.completion_tokens
              )} completion tokens, priced per model.`
            : `${formatTokens(usage.prompt_tokens)} prompt and ${formatTokens(
                usage.completion_tokens
              )} completion tokens. No model in this run has a configured price, so there is nothing to bill.`}
          {estimated
            ? ` ${formatCount(usage.estimated_calls, "call")} reported no usage, so those token counts are estimated from message length.`
            : ""}
        </p>
      </CardContent>
    </Card>
  );
}

/** One headline number and what it is. */
function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-muted-foreground text-xs">{label}</p>
      <p className="text-sm font-medium tabular-nums">{value}</p>
    </div>
  );
}

/**
 * One agent's share of the bill, with a bar for its share of the tokens.
 *
 * The bar is drawn from tokens rather than dollars so it stays meaningful on an
 * unpriced deployment, where every dollar figure is zero and a cost bar would be
 * a row of empty tracks. `aria-hidden` because it is a second encoding of the
 * figures beside it, not information of its own.
 */
function NodeRow({ node, total, priced }: { node: RunNodeUsage; total: number; priced: boolean }) {
  const share = total > 0 ? (node.tokens * 100) / total : 0;
  return (
    <div className="space-y-1.5" data-region="usage-node" data-node={node.node}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="min-w-0 flex-1 text-sm capitalize">
          {node.node}
          <span className="text-muted-foreground normal-case">
            {" "}
            · {formatCount(node.calls, "call")}
          </span>
        </span>
        <span className="text-sm tabular-nums">{formatTokens(node.tokens)}</span>
        <span className="text-muted-foreground w-20 text-right text-xs tabular-nums">
          {priced ? formatUsd(node.cost_usd) : "—"}
        </span>
      </div>
      <div className="bg-muted h-1.5 overflow-hidden rounded-full" aria-hidden="true">
        <div
          className={cn("bg-primary h-full rounded-full", node.tokens > 0 && "min-w-0.5")}
          style={{ width: `${share}%` }}
        />
      </div>
    </div>
  );
}
