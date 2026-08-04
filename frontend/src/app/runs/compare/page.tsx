import { Suspense } from "react";
import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { RunCompareSkeleton } from "@/components/skeletons";
import { RunCompare } from "@/components/runs/run-compare";

export const metadata: Metadata = { title: "Compare Runs · TrialGate" };

/**
 * Two runs side by side, deep-linked as `/runs/compare/?a=<id>&b=<id>` (#59).
 *
 * The Suspense boundary is required, not decorative: `useSearchParams` inside
 * `<RunCompare>` has no value during prerendering, and `next build` refuses to
 * export a page that reads it without one. Its fallback is the same skeleton the
 * component shows while it fetches, so the exported HTML a cold link paints first
 * and the hydrated page are one picture.
 */
export default function RunComparePage() {
  return (
    <>
      <PageHeader
        title="Compare Runs"
        description="Two screenings side by side — which criteria differ, and which patients they moved."
      />
      <Suspense fallback={<RunCompareSkeleton />}>
        <RunCompare />
      </Suspense>
    </>
  );
}
