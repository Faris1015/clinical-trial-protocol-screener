"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { NAV_ITEMS, isActiveRoute } from "@/lib/nav";
import { useAuth } from "@/components/AuthProvider";

/**
 * The nav item list itself, shared by the desktop sidebar and the mobile sheet.
 *
 * `onNavigate` lets the sheet close itself on selection; the sidebar omits it.
 */
export function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const { hasRole } = useAuth();

  return (
    <nav aria-label="Main" className="flex flex-col gap-1 p-2">
      {/* Role-gated entries are filtered out rather than disabled (#50): a
          reviewer has no use for a greyed-out Accounts link, and the page behind
          it would refuse them anyway. */}
      {NAV_ITEMS.filter(({ minRole }) => !minRole || hasRole(minRole)).map(
        ({ href, label, icon: Icon }) => {
          const active = isActiveRoute(pathname, href);
          return (
            <Link
              key={href}
              href={href}
              onClick={onNavigate}
              // aria-current is the accessible half of the highlight — screen
              // readers get the active route from this, not from the colour.
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                "focus-visible:ring-3 focus-visible:ring-sidebar-ring/50 outline-none",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground"
              )}
            >
              <Icon className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{label}</span>
              {/* The active marker is a shape, not only a colour change, so the
                current route survives a colour-blind or high-contrast view. */}
              {active && (
                <span
                  aria-hidden="true"
                  className="bg-sidebar-primary ml-auto h-4 w-0.5 shrink-0 rounded-full"
                />
              )}
            </Link>
          );
        }
      )}
    </nav>
  );
}
