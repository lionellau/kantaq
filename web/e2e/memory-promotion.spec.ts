/**
 * E13-T6 (MOD-19 / MOD-12) — the memory-promotion GUI loop, end to end.
 *
 * The v0.2 promote/approve/reject API loop shipped without a GUI (DEBT-28); this
 * spec drives the GUI a human now uses: create a team memory entry (it lands as
 * `draft`), promote it from the Memory page into the Inbox approval queue, then
 * Approve it in the Inbox "Memory promotions" tab — the entry becomes
 * `approved` and the queue returns to zero. Runs in the default `make e2e`
 * shared-server run; isolated from the other specs by a unique entry title.
 */

import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

const SHOTS = path.resolve(process.env.KANTAQ_E2E_SHOTS ?? "e2e/.e2e-shots");

function ownerToken(): string {
  const state = JSON.parse(readFileSync(path.resolve("e2e/.runtime/state.json"), "utf-8")) as {
    token: string;
  };
  return state.token;
}

async function connect(page: Page, token: string): Promise<void> {
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));

test("promote a memory entry, then approve it in the Inbox", async ({ page }) => {
  await connect(page, ownerToken());

  // A unique title keeps this entry distinct from the seed and the other specs.
  const title = `Promote me ${Date.now()}`;

  // 1. Create a team memory entry — v0.1 writes land it as review_status=draft.
  await page.goto("/memory");
  const createForm = page.getByRole("form", { name: "Create memory entry" });
  await createForm.getByLabel("Title").fill(title);
  await createForm.getByLabel("Body").fill("a team decision worth sharing");
  await createForm.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText(title)).toBeVisible();

  // 2. Promote it — a draft team entry transitions in place to `proposed` and
  //    routes into the Inbox approval queue.
  const row = page.getByRole("row").filter({ hasText: title });
  await row.getByRole("button", { name: "Promote to team" }).click();
  await expect(page.getByText(/awaiting approval in the Inbox/)).toBeVisible();
  await page.screenshot({ path: path.join(SHOTS, "memory-01-promoted.png"), fullPage: true });

  // 3. Approve it in the Inbox "Memory promotions" tab (human-only loop).
  await page.goto("/inbox");
  await page.getByRole("tab", { name: /Memory promotions/ }).click();
  const card = page.getByRole("listitem").filter({ hasText: title });
  await expect(card).toBeVisible();
  await page.screenshot({ path: path.join(SHOTS, "memory-02-inbox-queue.png"), fullPage: true });
  await card.getByRole("button", { name: "Approve" }).click();

  // 4. The entry is approved (it leaves the proposed queue).
  await expect(page.getByText(/now shared with the team/)).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: title })).toHaveCount(0);
  await page.screenshot({ path: path.join(SHOTS, "memory-03-approved.png"), fullPage: true });
});
