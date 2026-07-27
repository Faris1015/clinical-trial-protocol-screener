import { Construction } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Body for a nav destination whose feature hasn't been built yet.
 *
 * The shell (#48) ships all five nav routes so navigation and active-route
 * highlighting are real, but four of them have no feature behind them. Rather
 * than link to nothing — in a static export an unexported path is a hard 404 —
 * each renders this: what's coming, and the issue that will deliver it. The
 * skeleton rows stand in for the eventual content so the page reads as a
 * deliberate placeholder rather than an empty or broken screen.
 */
export function PendingRoute({
  title,
  issue,
  summary,
}: {
  title: string;
  issue: number;
  summary: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Construction className="text-status-warn size-4" aria-hidden="true" />
          {title} is not built yet
        </CardTitle>
        <CardDescription>
          {summary} Tracked in issue #{issue}; this route exists so the shell&apos;s navigation is
          complete.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-2" aria-hidden="true">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-5/6" />
          <Skeleton className="h-8 w-2/3" />
        </div>
      </CardContent>
    </Card>
  );
}
