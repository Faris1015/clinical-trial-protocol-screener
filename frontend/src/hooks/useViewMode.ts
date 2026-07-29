"use client";

import { useSyncExternalStore } from "react";

/**
 * How results are rendered (#52): the plain-language layer, or the technical one.
 *
 * "plain" is the default because the people reading a screening are not all
 * engineers — a coordinator wants "the patient's eGFR is 42, but the trial asks
 * for at least 60", not `egfr >= 60 (fail)`. "technical" is the same data with
 * rule ids, operators and source sentences, one click away.
 */
export type ViewMode = "plain" | "technical";

const STORAGE_KEY = "trialgate.view-mode";

/**
 * A module-level store rather than React state, for two reasons: the choice has
 * to persist across navigations (the app is a static export, so every route is a
 * fresh mount), and several toggles render at once — one per results card — which
 * must all show the same mode. `useSyncExternalStore` is React's own answer to
 * "subscribe to something outside React", and it keeps the prerender honest: it
 * renders the server snapshot, then re-renders with the stored value after
 * hydration instead of tripping a mismatch.
 */
let current: ViewMode | null = null;
const listeners = new Set<() => void>();

function read(): ViewMode {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "technical" ? "technical" : "plain";
  } catch {
    // Storage can throw outright in a privacy-locked browser; the default is
    // never worth breaking the page over.
    return "plain";
  }
}

function notify(): void {
  for (const listener of listeners) listener();
}

/**
 * Bound once for the page, not once per subscriber: several toggles subscribe to
 * this store, and a listener each would make one cross-tab write fan out into a
 * notification per subscriber.
 */
let storageBound = false;

function bindStorage(): void {
  if (storageBound) return;
  storageBound = true;
  window.addEventListener("storage", (e) => {
    // Another tab flipping the toggle invalidates our cache, not just its own.
    if (e.key === STORAGE_KEY) {
      current = null;
      notify();
    }
  });
}

function subscribe(onChange: () => void): () => void {
  bindStorage();
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

function snapshot(): ViewMode {
  if (current === null) current = read();
  return current;
}

/** Plain is what a static prerender must assume — nobody's preference is known yet. */
function serverSnapshot(): ViewMode {
  return "plain";
}

export function setViewMode(mode: ViewMode): void {
  current = mode;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Unpersisted is fine; the mode still applies for this session.
  }
  notify();
}

export function useViewMode(): { mode: ViewMode; technical: boolean; setMode: typeof setViewMode } {
  const mode = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
  return { mode, technical: mode === "technical", setMode: setViewMode };
}
