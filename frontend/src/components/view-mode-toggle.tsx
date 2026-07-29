"use client";

import { Button } from "@/components/ui/button";
import { useViewMode, type ViewMode } from "@/hooks/useViewMode";
import { cn } from "@/lib/utils";

const MODES: { value: ViewMode; label: string; hint: string }[] = [
  {
    value: "plain",
    label: "Plain language",
    hint: "Results written for a reviewer: what happened and why",
  },
  {
    value: "technical",
    label: "Technical",
    hint: "The same results with rule ids, operators and source sentences",
  },
];

/**
 * The plain-language / technical switch (#52).
 *
 * A two-button segmented control rather than a checkbox or an icon: both views
 * are legitimate destinations, and the labels say what they are — a reviewer
 * should never have to click to find out what the other side shows. It renders
 * next to the results it governs (the cohort table, the compliance findings)
 * instead of in the app chrome, so the scope of the switch is obvious; every
 * instance reads one shared mode, so they stay in step.
 */
export function ViewModeToggle({ className }: { className?: string }) {
  const { mode, setMode } = useViewMode();

  return (
    <div
      className={cn("flex w-fit gap-0.5 rounded-lg border border-border p-0.5", className)}
      role="group"
      aria-label="Result detail"
      data-region="view-mode-toggle"
      data-mode={mode}
    >
      {MODES.map((option) => {
        const selected = option.value === mode;
        return (
          <Button
            key={option.value}
            size="xs"
            variant={selected ? "secondary" : "ghost"}
            // aria-pressed, not a radio group: this toggles how the surrounding
            // card renders rather than entering a value into a form.
            aria-pressed={selected}
            title={option.hint}
            className={cn(!selected && "text-muted-foreground")}
            onClick={() => setMode(option.value)}
          >
            {option.label}
          </Button>
        );
      })}
    </div>
  );
}
