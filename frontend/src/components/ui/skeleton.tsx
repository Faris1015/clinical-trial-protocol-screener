import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      // `motion-safe:` rather than a bare `animate-pulse` (#49): a placeholder
      // that breathes forever is the kind of perpetual motion
      // `prefers-reduced-motion` exists to stop. Reduced motion keeps the shape
      // — the grey box still says "something is coming" — and drops the pulse.
      className={cn("motion-safe:animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  );
}

export { Skeleton };
