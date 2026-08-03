import { BarChart3, ClipboardCheck, FilePlus2, History, Scale, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Role } from "@/lib/api";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Issue that fills this route in; undefined once the route is real. */
  pendingIssue?: number;
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
 * The routes still carrying a `pendingIssue` are placeholders; that issue is
 * what those pages cite so a visitor knows the route is scaffolding rather than
 * broken.
 */
export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "New Screening", icon: FilePlus2 },
  { href: "/runs", label: "Past Runs", icon: History },
  { href: "/review", label: "Review Queue", icon: ClipboardCheck },
  { href: "/rules", label: "Rules", icon: Scale },
  { href: "/metrics", label: "Metrics", icon: BarChart3, pendingIssue: 58 },
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
