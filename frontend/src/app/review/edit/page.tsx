import { Suspense } from "react";
import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { CriteriaEditorSkeleton } from "@/components/skeletons";
import { CriteriaEditor } from "@/components/review/criteria-editor";

export const metadata: Metadata = { title: "Edit Criteria · TrialGate" };

/**
 * The edit-and-rerun page, deep-linked as `/review/edit/?id=<thread_id>` (#53).
 *
 * Same shape as the run-detail route: a query parameter because the app is a
 * static export, a Suspense boundary because `next build` refuses to export a
 * page whose component reads `useSearchParams` without one, and the editor's own
 * loading skeleton as that boundary's fallback (#49) so the exported HTML and
 * the hydrated page paint the same wait.
 */
export default function EditCriteriaPage() {
  return (
    <>
      <PageHeader
        title="Edit Criteria"
        description="Correct the extracted criteria against their source text, then re-run compliance."
      />
      <Suspense fallback={<CriteriaEditorSkeleton />}>
        <CriteriaEditor />
      </Suspense>
    </>
  );
}
