/**
 * E18-T3 — the browser session: one bearer token for the member's own runtime.
 *
 * The token comes from the local runtime (`kantaq token show`, or a member
 * invite) and is held in memory plus sessionStorage — surviving a reload but
 * not a closed tab, and never readable by other origins. The browser talks
 * only to its own runtime on 127.0.0.1 and never holds the device key (D-01);
 * this token is a member credential, not key material.
 *
 * A 401 from the API clears the session (the token was revoked or rotated),
 * which flips `useSession()` consumers into their disconnected state.
 */

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "kantaq.session.token";

let current: string | null = readStorage();
const listeners = new Set<() => void>();

function readStorage(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // storage unavailable (SSR, privacy mode): memory-only
  }
}

function notify(): void {
  for (const listener of listeners) {
    listener();
  }
}

export function getToken(): string | null {
  return current;
}

export function setToken(token: string): void {
  const trimmed = token.trim();
  if (!trimmed) {
    return;
  }
  current = trimmed;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, trimmed);
  } catch {
    // memory-only fallback
  }
  notify();
}

export function clearToken(): void {
  current = null;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing stored
  }
  notify();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** React hook: re-renders when the session connects or disconnects. */
export function useSession(): { connected: boolean } {
  const token = useSyncExternalStore(subscribe, getToken);
  return { connected: token !== null };
}
