"use client";

import { LogOut, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import { useAuth } from "@/components/AuthProvider";

/**
 * Account menu: who you're signed in as, your role, and sign out (#50).
 *
 * Fills the slot #48 left as a disabled placeholder. Renders nothing without a
 * session, so the login page — which sits inside this same shell — doesn't offer
 * an account control for an account that doesn't exist yet.
 *
 * The role shown here is presentation only. Every `/api` route enforces the role
 * itself (401/403, see backend/app/main.py), so what this menu displays can never
 * be the reason an action is or isn't permitted.
 */
export function UserMenu() {
  const { principal, signOut } = useAuth();

  if (!principal) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant="ghost" size="icon" aria-label="Account menu" />}
      >
        <UserRound className="size-4" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {/* The Group wrapper is required, not stylistic: DropdownMenuLabel is
            Base UI's Menu.GroupLabel, which throws "MenuGroupContext is missing"
            outside a Menu.Group — and that error takes the whole menu down, so it
            never opens. (#48's placeholder menu had the same shape and threw too.) */}
        <DropdownMenuGroup>
          <DropdownMenuLabel className="flex flex-col items-start gap-1.5">
            {/* `break-all`: an email is one unbroken token, so without it a long
                address overflows the menu rather than wrapping inside it. */}
            <span className="text-sm leading-tight font-medium break-all">{principal.email}</span>
            <Badge variant="secondary" className="text-[0.65rem] uppercase">
              {principal.role}
            </Badge>
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => void signOut()}>
          <LogOut className="size-4" aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
