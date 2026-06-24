/**
 * MOD-11/12 — light/dark theme control.
 *
 * The design tokens (src/index.css) carry both light and dark values via
 * `light-dark()`, resolved by each element's `color-scheme`. The default `:root`
 * follows the OS. This module lets a person override that: setting `data-theme`
 * on `<html>` flips `color-scheme` (and therefore every token) to a fixed mode,
 * persisted in localStorage so the choice survives reloads.
 *
 * First paint is handled by the tiny inline script in index.html (it reads the
 * stored value before React mounts, so a returning dark-mode user never flashes
 * light). This module is the React-side counterpart that keeps the toggle and
 * the attribute in sync.
 */

import { useCallback, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "kantaq-theme";

/** The OS preference; "light" when it can't be read (e.g. jsdom under tests). */
export function systemTheme(): Theme {
  const mql =
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null;
  return mql?.matches ? "dark" : "light";
}

/** The explicit choice a person made, or null if they've never overridden. */
export function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

/** What the UI is actually showing: an explicit choice, else the OS preference. */
export function activeTheme(): Theme {
  return storedTheme() ?? systemTheme();
}

/** Force `color-scheme` (and every token) to a fixed mode on the document. */
export function applyTheme(theme: Theme): void {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", theme);
  }
}

/** Persist an explicit choice and apply it immediately. */
export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // private mode / storage disabled: the toggle still works for this session.
  }
  applyTheme(theme);
}

/**
 * Toggle state for the sidebar control. Initial value matches the inline FOUC
 * script (stored choice, else OS), so the button label is right on first paint.
 */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setThemeState] = useState<Theme>(() => activeTheme());
  const toggle = useCallback(() => {
    setThemeState((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      setTheme(next);
      return next;
    });
  }, []);
  return { theme, toggle };
}
