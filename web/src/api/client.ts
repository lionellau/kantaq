/**
 * E18-T2 — the typed API client (D-08).
 *
 * `paths` is generated from the runtime's OpenAPI document (`pnpm gen:api`,
 * gated in CI), so every call site is checked against the real API: a renamed
 * field or path breaks the build instead of a screen. openapi-fetch adds ~6 kB
 * and zero codegen at runtime.
 *
 * Auth wiring (E18-T3): every request carries the session bearer token; a 401
 * clears the session so the UI falls back to its connect state. The base URL
 * is same-origin — the SPA is served by the member's own runtime and talks to
 * nothing else (D-01).
 */

import createClient, { type Middleware } from "openapi-fetch";
import { clearToken, getToken } from "../lib/session";
import type { paths } from "./schema";

const auth: Middleware = {
  onRequest({ request }) {
    const token = getToken();
    if (token !== null) {
      request.headers.set("Authorization", `Bearer ${token}`);
    }
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) {
      clearToken();
    }
    return response;
  },
};

function sameOrigin(): string {
  // The serving runtime's own origin. `Request` needs an absolute URL outside
  // a real browser (node/undici), so fall back to the default runtime bind.
  if (typeof window !== "undefined" && window.location?.origin) {
    return window.location.origin;
  }
  return "http://127.0.0.1:3939";
}

export function createApiClient(baseUrl: string = sameOrigin()) {
  const client = createClient<paths>({ baseUrl });
  client.use(auth);
  return client;
}

/** The app-wide client instance (same origin as the serving runtime). */
export const api = createApiClient();
