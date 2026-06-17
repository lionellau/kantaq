/**
 * The hero-flow end-to-end (MOD-11/MOD-12; harness standard §4, UI profile).
 *
 * `webServer` boots a disposable real runtime (migrations → bootstrap Owner →
 * seeded proposal → uvicorn serving the built UI) via scripts/e2e_server.py;
 * the specs then drive the same SPA a member uses, token-paste and all.
 * Build first: `pnpm build` (the runtime serves web/dist).
 */

import { defineConfig } from "@playwright/test";

const PORT = Number(process.env.KANTAQ_E2E_PORT ?? "39391");

export default defineConfig({
  testDir: "e2e",
  // The UAT specs (uat-walkthrough, uat-roles) each mutate the seeded state and
  // are designed to run in ISOLATION (their own fresh server boot), so the
  // default shared-server `make e2e` run excludes them. Run them explicitly with
  // KANTAQ_UAT=1 + the file path, e.g.
  //   KANTAQ_UAT=1 pnpm -C web exec playwright test e2e/uat-roles.spec.ts
  // (see docs/test/sprint-1-7-uat-plan.md).
  testIgnore: process.env.KANTAQ_UAT === "1" ? [] : /uat-.*\.spec\.ts/,
  timeout: 30_000,
  // One worker: both specs share the seeded runtime state.
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  webServer: {
    command: "cd .. && uv run python scripts/e2e_server.py",
    url: `http://127.0.0.1:${PORT}/healthz`,
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
