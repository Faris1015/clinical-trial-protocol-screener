import {
  BarChart3,
  ClipboardCheck,
  FilePlus2,
  HeartPulse,
  History,
  Scale,
  ScrollText,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Role } from "@/lib/api";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /**
   * Minimum role that sees this entry (#50). Presentation only — the route's data
   * is protected by the API's own 403, so hiding an entry never *is* the
   * enforcement, it just avoids showing someone a door they can't open.
   */
  minRole?: Role;
};

/**
 * The product's primary navigation, in sidebar order. One list, rendered by both
 * the desktop sidebar and the mobile sheet, so the two can't drift.
 *
 * Every entry now has a feature behind it — the shell (#48) shipped these routes
 * as placeholders citing the issue that would fill them, and #58 was the last one
 * (which is why `components/shell/pending-route.tsx` is gone).
 */
export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "New Screening", icon: FilePlus2 },
  { href: "/runs", label: "Past Runs", icon: History },
  // No `minRole` (#96): the cohort and a patient's trial matches reach no further
  // into patient data than a run's own cohort table already does, and that is on
  // every run detail page a reviewer can open.
  { href: "/patients", label: "Cohort", icon: HeartPulse },
  { href: "/review", label: "Review Queue", icon: ClipboardCheck },
  { href: "/rules", label: "Rules", icon: Scale },
  { href: "/metrics", label: "Metrics", icon: BarChart3 },
  // No `minRole` (#98): an admin reads the whole org's decisions and a reviewer
  // reads their own, so both have something behind the door. The scoping is the
  // API's, applied in the query — hiding the entry from reviewers would take away
  // a page they are entitled to rather than protect anything.
  { href: "/audit", label: "Audit Log", icon: ScrollText },
  { href: "/admin", label: "Accounts", icon: Users, minRole: "admin" },
];

/**
 * Drop a trailing slash so "/runs" and "/runs/" compare equal.
 *
 * Not cosmetic: `next.config.ts` sets `trailingSlash: true`, so the exported app
 * is served from per-route directories and the browser's path carries the slash
 * that `href="/runs"` does not. Comparing raw strings highlights the active item
 * in `next dev` and silently fails in the production export.
 */
function normalize(path: string): string {
  return path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
}

/**
 * Whether `href` is the section the user is currently in. "/" has to match
 * exactly — as a prefix it would match every route — while section roots also
 * match their children, so `/runs/<id>` (#51) keeps "Past Runs" lit.
 */
export function isActiveRoute(pathname: string, href: string): boolean {
  const path = normalize(pathname);
  const target = normalize(href);
  if (target === "/") return path === "/";
  return path === target || path.startsWith(`${target}/`);
}
