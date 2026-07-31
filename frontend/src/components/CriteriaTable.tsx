import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { CriteriaSchema } from "@/types";

/** Selecting a criterion's source sentence, or null to clear the selection (#54). */
type SelectSource = (source: string | null) => void;

/**
 * One criterion, carrying the protocol sentence it was read out of.
 *
 * The sentence is a Tooltip rather than a `title` attribute — invisible on touch,
 * unreadable to a screen reader. When the table is wired to a protocol view (#54)
 * the chip also becomes a button: clicking it highlights that sentence in the
 * document, and clicking the selected chip again clears the highlight. Without an
 * `onSelect` there is nothing to select *into*, so the chip stays inert markup
 * rather than a button that does nothing.
 */
function CriterionChip({
  label,
  source,
  variant,
  kind,
  mono = false,
  selected,
  onSelect,
}: {
  label: React.ReactNode;
  source: string;
  variant: "pass" | "fail";
  kind: "inclusion" | "exclusion";
  mono?: boolean;
  selected: boolean;
  onSelect?: SelectSource;
}) {
  const className = cn(
    mono && "font-mono",
    onSelect ? "cursor-pointer" : "cursor-help",
    // Ring rather than a colour change: the chip's colour is already saying
    // inclusion vs exclusion, and a selected chip must not read as a different
    // kind of criterion.
    selected && "ring-ring ring-2 ring-offset-1"
  );
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge
            variant={variant}
            className={className}
            data-kind={kind}
            data-selected={selected || undefined}
            render={
              onSelect ? (
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => onSelect(selected ? null : source)}
                />
              ) : undefined
            }
          />
        }
      >
        {label}
      </TooltipTrigger>
      <TooltipContent>{source}</TooltipContent>
    </Tooltip>
  );
}

/**
 * The parsed criteria, as chips grouped by inclusion vs exclusion.
 *
 * `onSelectSource` turns provenance from a tooltip into a place in the document:
 * the chips become the selector for `ProtocolView`, which highlights the sentence
 * a criterion came from (#54). Both are optional, so the table still renders
 * standalone.
 */
export function CriteriaTable({
  criteria,
  selectedSource = null,
  onSelectSource,
}: {
  criteria: CriteriaSchema | null;
  selectedSource?: string | null;
  onSelectSource?: SelectSource;
}) {
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
            <CriterionChip
              key={`q${i}`}
              source={c.source_text}
              variant={c.kind === "inclusion" ? "pass" : "fail"}
              kind={c.kind}
              mono
              selected={selectedSource === c.source_text}
              onSelect={onSelectSource}
              label={
                <>
                  {c.attribute} {c.operator} {c.value}
                  {c.operator === "between" ? `–${c.value_high}` : ""} {c.unit}
                </>
              }
            />
          ))}
          {cat.map((c, i) => (
            <CriterionChip
              key={`c${i}`}
              source={c.source_text}
              variant={c.kind === "inclusion" ? "pass" : "fail"}
              kind={c.kind}
              selected={selectedSource === c.source_text}
              onSelect={onSelectSource}
              label={`${c.negated ? "¬ " : ""}${c.value}`}
            />
          ))}
        </div>
        {unparseable.length > 0 && (
          <p className="text-status-warn text-xs" data-region="criteria-unparseable">
            <span className="font-semibold">Unparseable:</span>{" "}
            {unparseable.map((sentence, i) => (
              <span key={i}>
                {i > 0 && "; "}
                {/* Also selectable (#54): a sentence the parser gave up on is the
                    one a reviewer most needs to read in its original context. */}
                {onSelectSource ? (
                  <button
                    type="button"
                    aria-pressed={selectedSource === sentence}
                    onClick={() => onSelectSource(selectedSource === sentence ? null : sentence)}
                    className={cn(
                      "cursor-pointer text-left underline-offset-4 hover:underline",
                      selectedSource === sentence && "font-semibold underline"
                    )}
                  >
                    {sentence}
                  </button>
                ) : (
                  sentence
                )}
              </span>
            ))}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
