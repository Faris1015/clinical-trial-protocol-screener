import { Brand } from "@/components/shell/brand";
import { MobileNav } from "@/components/shell/mobile-nav";
import { UserMenu } from "@/components/shell/user-menu";
import { ThemeToggle } from "@/components/theme-toggle";

/**
 * Top bar: the mobile nav trigger and wordmark on small screens (where the
 * sidebar is hidden and would otherwise take the brand with it), then the theme
 * toggle and account menu.
 *
 * `sticky` so the account menu and theme switch stay reachable while a long
 * cohort table scrolls underneath.
 */
export function TopBar() {
  return (
    <header className="bg-background/80 border-border sticky top-0 z-40 flex h-14 shrink-0 items-center gap-2 border-b px-3 backdrop-blur md:px-6">
      <MobileNav />
      <Brand className="md:hidden" />
      <div className="ml-auto flex items-center gap-1">
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
