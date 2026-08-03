import { Suspense } from "react";
import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { RulesSkeleton } from "@/components/skeletons";
import { RulesIndex } from "@/components/rules/rules-index";

export const metadata: Metadata = { title: "Rules · TrialGate" };

/**
 * The compliance rules database (#57), deep-linked as `/rules/?rule=<id>` from
 * every Critic finding.
 *
 * Server component only so the route can export `metadata`; the rules themselves
 * come from a client-side fetch against the session cookie. The Suspense
 * boundary is required rather than decorative — `<RulesIndex>` reads
 * `useSearchParams` for the linked rule, and `next build` refuses to export a
 * page that does so without one. Its fallback is the same skeleton the component
 * shows while fetching, so a cold deep link paints one picture, not two.
 */
export default function RulesPage() {
  return (
    <>
      <PageHeader
        title="Rules"
        description="The compliance rules the regulatory critic checks each protocol against."
      />
      <Suspense fallback={<RulesSkeleton />}>
        <RulesIndex />
      </Suspense>
    </>
  );
}
