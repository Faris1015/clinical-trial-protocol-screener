"use client";

import { LazyMotion, MotionConfig, domAnimation, m } from "motion/react";
import type { HTMLMotionProps, Transition, Variants } from "motion/react";

/**
 * The app's motion primitives (#49) — one provider, one curve, one entrance.
 *
 * Two deliberate constraints live here rather than in each call site:
 *
 * 1. `LazyMotion features={domAnimation} strict` instead of the full `motion`
 *    component. `domAnimation` is the subset this product actually uses —
 *    opacity/transform tweens, variants, exit animations — and leaves the rest
 *    of the library out of a static export that otherwise ships very little JS.
 *    `strict` makes the cheap mistake loud: importing `motion.div` anywhere
 *    throws at render instead of silently pulling the full bundle back in, so
 *    every animated element in the codebase is an `m.*` one. Layout animations
 *    are *not* in this feature set — deliberately, since the surfaces below
 *    stream in mid-run and animating layout would move content a reviewer is
 *    reading.
 *
 * 2. `reducedMotion="user"` is the accessibility contract for the whole app, set
 *    once at the root: when the OS asks for reduced motion every transform and
 *    layout animation is dropped and only the opacity cross-fade remains, which
 *    is the distinction `prefers-reduced-motion` is actually about — vestibular
 *    triggers are travel and scale, not a fade. Anything that *loops* is a
 *    stronger case still and is switched off entirely at its call site via
 *    `useReducedMotion` (see AgentCard's activity bar), because a permanent
 *    animation is exactly what the setting is meant to stop.
 */

/** Entrance curve: fast out of the gate, long settle. Shared by everything. */
const ENTER: Transition = { duration: 0.28, ease: [0.16, 1, 0.3, 1] };

/** Exits are quicker than entrances — a leaving element shouldn't be read. */
const EXIT: Transition = { duration: 0.14, ease: "easeIn" };

/**
 * The one entrance: fade up a short distance. `gone` drifts up rather than back
 * down so a skeleton being replaced by real content reads as one upward motion
 * instead of two elements passing each other.
 */
const reveal: Variants = {
  hidden: { opacity: 0, y: 8 },
  shown: { opacity: 1, y: 0, transition: ENTER },
  gone: { opacity: 0, y: -4, transition: EXIT },
};

export function MotionProvider({ children }: { children: React.ReactNode }) {
  return (
    <LazyMotion features={domAnimation} strict>
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </LazyMotion>
  );
}

/**
 * One block that fades up as it arrives — a streamed card, a banner, a skeleton
 * being swapped for the thing it stood in for.
 *
 * Standalone it animates on mount; nested inside a motion element that drives
 * the same `hidden`/`shown` states it inherits them, which is how a group would
 * be staggered. Under an `AnimatePresence` it also animates out; without one the
 * `gone` variant is simply never used.
 */
export function Reveal({ children, ...rest }: HTMLMotionProps<"div">) {
  return (
    <m.div variants={reveal} initial="hidden" animate="shown" exit="gone" {...rest}>
      {children}
    </m.div>
  );
}
