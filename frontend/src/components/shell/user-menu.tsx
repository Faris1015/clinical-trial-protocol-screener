"use client";

import { LogOut, Settings, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * Account menu placeholder.
 *
 * There is no auth yet — that's B1 (#50). Rather than mock a signed-in user,
 * the items are rendered `disabled` and the menu says where identity comes from,
 * so nothing here reads as a working session. #50 replaces the label with the
 * real reviewer/admin identity and enables the items.
 */
export function UserMenu() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant="ghost" size="icon" aria-label="Account menu" />}
      >
        <UserRound className="size-4" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="text-muted-foreground text-xs font-normal">
          Sign-in arrives with auth (#50)
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled>
          <UserRound className="size-4" aria-hidden="true" />
          Profile
        </DropdownMenuItem>
        <DropdownMenuItem disabled>
          <Settings className="size-4" aria-hidden="true" />
          Settings
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled>
          <LogOut className="size-4" aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
