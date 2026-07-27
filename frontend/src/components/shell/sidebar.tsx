import { Brand } from "@/components/shell/brand";
import { NavLinks } from "@/components/shell/nav-links";

/**
 * Desktop sidebar. Hidden below `md`, where the same nav is reached through the
 * top bar's sheet — see MobileNav.
 *
 * `sticky top-0 h-svh` rather than `fixed`: it keeps the sidebar in normal flow
 * so the main column's width is the flex remainder, with no margin offset to keep
 * in sync with the sidebar's width.
 */
export function Sidebar() {
  return (
    <aside className="bg-sidebar border-sidebar-border sticky top-0 hidden h-svh w-60 shrink-0 flex-col border-r md:flex">
      <div className="border-sidebar-border flex h-14 items-center border-b px-4">
        <Brand />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NavLinks />
      </div>
      <div className="border-sidebar-border text-muted-foreground border-t p-3 text-xs">
        Multi-agent · LangGraph · human-in-the-loop
      </div>
    </aside>
  );
}
