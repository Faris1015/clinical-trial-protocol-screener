import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const metadata: Metadata = { title: "Design kit · TrialGate" };

const BUTTON_VARIANTS = [
  "default",
  "secondary",
  "outline",
  "ghost",
  "destructive",
  "link",
] as const;
const BUTTON_SIZES = ["xs", "sm", "default", "lg"] as const;
const BADGE_VARIANTS = ["default", "secondary", "outline", "ghost", "destructive", "link"] as const;
const STATUS_VARIANTS = ["pass", "warn", "fail"] as const;

const TOKENS = [
  { name: "background / foreground", cls: "bg-background text-foreground border-border border" },
  { name: "card / card-foreground", cls: "bg-card text-card-foreground border-border border" },
  { name: "primary", cls: "bg-primary text-primary-foreground" },
  { name: "secondary", cls: "bg-secondary text-secondary-foreground" },
  { name: "muted", cls: "bg-muted text-muted-foreground" },
  { name: "accent (hover surface)", cls: "bg-accent text-accent-foreground" },
  { name: "destructive", cls: "bg-destructive text-white" },
  { name: "status-pass", cls: "bg-status-pass text-white" },
  { name: "status-warn", cls: "bg-status-warn text-white" },
  { name: "status-fail", cls: "bg-status-fail text-white" },
];

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">{children}</CardContent>
    </Card>
  );
}

/**
 * The one place the component kit is documented (acceptance criterion for #48).
 *
 * Deliberately a live page rather than a markdown file: it renders the real
 * components, so a broken variant or a token that fails in one theme shows up
 * here instead of drifting silently out of date. Switch the theme with the top
 * bar toggle to check both.
 *
 * Not linked from the sidebar — it's for contributors, not reviewers using the
 * product. Reachable at /design.
 */
export default function DesignPage() {
  return (
    <>
      <PageHeader
        title="Design kit"
        description="Every component and variant the product is built from, rendered live. Toggle the theme in the top bar to check both."
      />

      <div className="space-y-4">
        <Section
          title="Colour tokens"
          description="Semantic tokens from globals.css. shadcn's `accent` is the subtle hover surface — the brand blue is `primary`. The `status-*` trio carries clinical meaning and is the only correct source for pass/fail/indeterminate."
        >
          <div className="grid gap-2 sm:grid-cols-2">
            {TOKENS.map((t) => (
              <div
                key={t.name}
                className={`flex items-center justify-between rounded-md px-3 py-2 text-xs ${t.cls}`}
              >
                <span className="font-medium">{t.name}</span>
              </div>
            ))}
          </div>
        </Section>

        <Section
          title="Button"
          description="Actions. `default` is the primary action — one per view. `destructive` is tinted rather than solid, so a dangerous action reads as serious without dominating the page."
        >
          <div className="flex flex-wrap items-center gap-2">
            {BUTTON_VARIANTS.map((v) => (
              <Button key={v} variant={v}>
                {v}
              </Button>
            ))}
          </div>
          <Separator />
          <div className="flex flex-wrap items-center gap-2">
            {BUTTON_SIZES.map((s) => (
              <Button key={s} size={s}>
                size {s}
              </Button>
            ))}
            <Button disabled>disabled</Button>
          </div>
        </Section>

        <Section
          title="Badge"
          description="Status and metadata. The stock variants cover neutral labelling; pass / warn / fail are TrialGate additions bound to the clinical status tokens."
        >
          <div className="flex flex-wrap items-center gap-2">
            {BADGE_VARIANTS.map((v) => (
              <Badge key={v} variant={v}>
                {v}
              </Badge>
            ))}
          </div>
          <Separator />
          <div className="space-y-2">
            <p className="text-muted-foreground text-xs">
              Clinical semantics — used by the criteria chips (inclusion → pass, exclusion → fail)
              and the cohort table (eligible / needs review / ineligible).
            </p>
            <div className="flex flex-wrap items-center gap-2">
              {STATUS_VARIANTS.map((v) => (
                <Badge key={v} variant={v}>
                  {v}
                </Badge>
              ))}
            </div>
          </div>
        </Section>

        <Section
          title="Card"
          description="The default surface for every grouped block: pipeline agents, criteria, the cohort, and the approval and failure banners."
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">With header</CardTitle>
                <CardDescription>Title, description, content.</CardDescription>
              </CardHeader>
              <CardContent className="text-muted-foreground text-sm">Body content.</CardContent>
            </Card>
            <Card className="border-primary/40 bg-primary/10">
              <CardContent className="text-sm">
                Tinted variant — how the approval gate is rendered.
              </CardContent>
            </Card>
          </div>
        </Section>

        <Section
          title="Table"
          description="Dense data. The component wraps itself in an overflow-x container, so a wide table scrolls inside its own box instead of scrolling the page sideways — provided its flex ancestors carry min-w-0."
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Patient</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Note</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell className="font-mono text-xs">P-0001</TableCell>
                <TableCell>
                  <Badge variant="pass">eligible</Badge>
                </TableCell>
                <TableCell className="text-muted-foreground text-sm">All criteria met</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-mono text-xs">P-0002</TableCell>
                <TableCell>
                  <Badge variant="warn">review</Badge>
                </TableCell>
                <TableCell className="text-muted-foreground text-sm">
                  Biomarker not recorded
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-mono text-xs">P-0003</TableCell>
                <TableCell>
                  <Badge variant="fail">ineligible</Badge>
                </TableCell>
                <TableCell className="text-muted-foreground text-sm">Age below minimum</TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </Section>

        <Section
          title="Skeleton"
          description="Loading and not-yet-built placeholders. Animated pulse on the muted token; #49 replaces the pipeline's idle state with these."
        >
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-5/6" />
            <Skeleton className="h-8 w-2/3" />
          </div>
        </Section>

        <Section
          title="Separator"
          description="Divides related groups inside one surface, in place of nesting another card."
        >
          <div className="text-muted-foreground text-sm">Above</div>
          <Separator />
          <div className="text-muted-foreground text-sm">Below</div>
        </Section>
      </div>
    </>
  );
}
