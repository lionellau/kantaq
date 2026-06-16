/**
 * E20-T5 — resolve a seeded sync conflict end to end against a real runtime.
 *
 * The runtime is booted by scripts/e2e_server.py with an open conflict_record
 * folded into the local replica and a FakeBackend-backed resolve engine
 * injected, so POST /v1/conflicts/{id}/resolve runs the real CAS path. This
 * backs the v0.2 exit criterion "a maintainer resolves a conflict from the
 * Inbox and the resolution is a new audited event".
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

interface E2EState {
  base_url: string;
  token: string;
  conflict_id: string;
  conflict_ticket_id: string;
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

test("review and resolve a seeded sync conflict from the Inbox", async ({ page }) => {
  const state = loadState();
  await connect(page, state.token);

  await page.goto("/inbox");
  await page.getByRole("tab", { name: /Sync conflicts/ }).click();

  // The conflict renders its field path and both candidate values.
  await expect(page.getByText(new RegExp(`tickets/${state.conflict_ticket_id}`))).toBeVisible();
  await expect(page.getByTestId("conflict-keep-a")).toHaveText("doing");
  await expect(page.getByTestId("conflict-keep-b")).toHaveText("todo");

  // Pick a side → the real resolve_conflict CAS commits, the record resolves.
  await page.getByRole("button", { name: "Keep A" }).click();
  await expect(page.getByText(/Resolved —/)).toBeVisible();

  // It leaves the open queue (the next poll re-reads live, no cache).
  await expect(page.getByText(/No sync conflicts/)).toBeVisible();
});
