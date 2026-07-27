"use client";

import { useState } from "react";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Brand } from "@/components/shell/brand";
import { NavLinks } from "@/components/shell/nav-links";

/**
 * The sidebar's under-`md` counterpart: the same NavLinks inside a left sheet.
 *
 * Open state is controlled so following a link can close it — the exported app
 * navigates client-side, so without this the sheet would stay open over the page
 * the user just asked for.
 */
export function MobileNav() {
  const [open, setOpen] = useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger
        render={
          <Button variant="ghost" size="icon" className="md:hidden" aria-label="Open navigation" />
        }
      >
        <Menu className="size-4" aria-hidden="true" />
      </SheetTrigger>
      <SheetContent side="left" className="bg-sidebar w-64 p-0">
        <SheetHeader className="border-sidebar-border h-14 border-b px-4">
          <SheetTitle className="text-left">
            <Brand />
          </SheetTitle>
        </SheetHeader>
        <NavLinks onNavigate={() => setOpen(false)} />
      </SheetContent>
    </Sheet>
  );
}
