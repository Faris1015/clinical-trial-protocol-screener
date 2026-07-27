import Link from "next/link";
import { ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * TrialGate wordmark, linking home. Used in the sidebar header and the mobile
 * sheet; the top bar shows it only on small screens, where the sidebar is hidden.
 */
export function Brand({ className }: { className?: string }) {
  return (
    <Link
      href="/"
      className={cn(
        "flex items-center gap-2 rounded-md font-semibold tracking-tight",
        "focus-visible:ring-3 focus-visible:ring-ring/50 outline-none",
        className
      )}
    >
      <span className="bg-primary text-primary-foreground flex size-7 shrink-0 items-center justify-center rounded-md">
        <ShieldCheck className="size-4" aria-hidden="true" />
      </span>
      <span>TrialGate</span>
    </Link>
  );
}
