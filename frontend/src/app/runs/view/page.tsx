import { Suspense } from "react";
import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { RunDetailSkeleton } from "@/components/skeletons";
import { RunDetail } from "@/components/runs/run-detail";

export const metadata: Metadata = { title: "Run Detail · TrialGate" };

/**
 * A single past run, deep-linked as `/runs/view/?id=<thread_id>` (#51).
 *
 * The Suspense boundary is required, not decorative: `useSearchParams` inside
 * `<RunDetail>` has no value during prerendering, and `next build` refuses to
 * export a page that reads it without one.
 *
 * Its fallback is the same skeleton `<RunDetail>` shows while it fetches (#49),
 * so the exported HTML — which is what a cold deep link paints first — and the
 * hydrated page are the same picture, and the wait doesn't visibly restart.
 */
export default function RunDetailPage() {
  return (
    <>
      <PageHeader title="Run Detail" description="A past screening, replayed read-only." />
      <Suspense fallback={<RunDetailSkeleton />}>
        <RunDetail />
      </Suspense>
    </>
  );
}
