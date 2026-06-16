/**
 * E20-T5 / MOD-27 — the Settings → Sync metrics dashboard renders against a real
 * runtime (the "headless QA" half of the MOD-27 dashboard test plan).
 *
 * The runtime is the same one scripts/e2e_server.py boots for the conflict flow,
 * so this spec reuses the shared webServer and just connects + opens the Sync
 * page. That runtime runs HUB_MODE=local, so the capacity block is the local-only
 * note here; the supabase-tier gauge, the "View billing ↗" deep-link, and the
 * idle-pause banner are pinned by the component vitest suite (Sync.test.tsx),
 * which can mock a supabase-tier summary this local runtime does not produce.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

interface E2EState {
  token: string;
}

function loadState(): E2EState {
  const file = path.resolve("e2e/.runtime/state.json");
  return JSON.parse(readFileSync(file, "utf-8")) as E2EState;
}

async function connect(page: Page, token: string): Promise<void> {
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

test("the metrics dashboard renders on Settings → Sync against the live runtime", async ({
  page,
}) => {
  const state = loadState();
  await connect(page, state.token);

  await page.goto("/settings/sync");

  // The dashboard mounts and fetched /v1/metrics/summary from the real runtime.
  await expect(page.getByTestId("metrics-dashboard")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Backend capacity" })).toBeVisible();

  // Per-actor agent observability, incl. the est_tokens payload-proxy column that
  // the MOD-08 gateway tally feeds (labelled a proxy, not model tokens).
  await expect(page.getByRole("heading", { name: /Agent activity/ })).toBeVisible();
  await expect(page.getByText(/payload-size proxy/)).toBeVisible();

  // The retention status section renders too.
  await expect(page.getByRole("heading", { name: "Retention" })).toBeVisible();

  // This runtime boots HUB_MODE=local, so the capacity block is the local-only note.
  await expect(page.getByText(/Local-only workspace/)).toBeVisible();
});
