import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { CriteriaSchema } from "@/types";

/**
 * The parsed criteria, as chips grouped by inclusion vs exclusion.
 *
 * Each chip carries the protocol sentence it came from. That was a `title`
 * attribute before, which is invisible on touch and unreadable to a screen
 * reader; it's a Tooltip now, so the provenance is actually reachable. Full
 * criterion→source_text highlighting is #54.
 */
export function CriteriaTable({ criteria }: { criteria: CriteriaSchema | null }) {
  if (!criteria) return null;

  const quant = [
    ...(criteria.inclusion_quantitative ?? []).map((c) => ({ ...c, kind: "inclusion" as const })),
    ...(criteria.exclusion_quantitative ?? []).map((c) => ({ ...c, kind: "exclusion" as const })),
  ];
  const cat = [
    ...(criteria.inclusion_categorical ?? []).map((c) => ({ ...c, kind: "inclusion" as const })),
    ...(criteria.exclusion_categorical ?? []).map((c) => ({ ...c, kind: "exclusion" as const })),
  ];
  const unparseable = criteria.unparseable ?? [];

  return (
    <Card data-region="criteria">
      <CardHeader>
        <CardTitle className="text-base">{criteria.trial_title || "Parsed criteria"}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-1.5">
          {quant.map((c, i) => (
            <Tooltip key={`q${i}`}>
              <TooltipTrigger
                render={
                  <Badge
                    variant={c.kind === "inclusion" ? "pass" : "fail"}
                    className="cursor-help font-mono"
                    data-kind={c.kind}
                  />
                }
              >
                {c.attribute} {c.operator} {c.value}
                {c.operator === "between" ? `–${c.value_high}` : ""} {c.unit}
              </TooltipTrigger>
              <TooltipContent>{c.source_text}</TooltipContent>
            </Tooltip>
          ))}
          {cat.map((c, i) => (
            <Tooltip key={`c${i}`}>
              <TooltipTrigger
                render={
                  <Badge
                    variant={c.kind === "inclusion" ? "pass" : "fail"}
                    className="cursor-help"
                    data-kind={c.kind}
                  />
                }
              >
                {c.negated ? "¬ " : ""}
                {c.value}
              </TooltipTrigger>
              <TooltipContent>{c.source_text}</TooltipContent>
            </Tooltip>
          ))}
        </div>
        {unparseable.length > 0 && (
          <p className="text-status-warn text-xs">
            <span className="font-semibold">Unparseable:</span> {unparseable.join("; ")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
