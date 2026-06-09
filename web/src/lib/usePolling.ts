import { useEffect, useRef } from "react";

/**
 * E22-T3 — the 2-second polling refresh primitive (MOD-14).
 *
 * Calls `callback` every `intervalMs` while `enabled`. In online sync modes the
 * runtime exposes synced changes that the UI pulls on this cadence; until the
 * sync endpoints land (Sprint 2) this is the reusable timer primitive the views
 * build on. Uses a ref so a changing callback identity does not reset the timer.
 */
export function usePolling(callback: () => void, intervalMs = 2000, enabled = true): void {
  const saved = useRef(callback);
  saved.current = callback;

  useEffect(() => {
    if (!enabled) {
      return;
    }
    const id = setInterval(() => saved.current(), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, enabled]);
}
