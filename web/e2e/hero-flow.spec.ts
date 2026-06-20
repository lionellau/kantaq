/**
 * The sprint-2 hero flow, end to end against a real runtime:
 * create-then-view-ticket (MOD-11) and approve-a-proposal (MOD-12).
 * These back the dogfood-gate demo steps #1, #3, and #4.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

interface E2EState {
  base_url: string;
  token: string;
  ticket_id: string;
  project_id: string;
}

function loadState(): E2EState {
  // Written by scripts/e2e_server.py before the server starts listening.
  const file = path.resolve("e2e/.runtime/state.json");
  return JSON.parse(readFileSync(file, "utf-8")) as E2EState;
}

async function connect(page: Page, token: string): Promise<void> {
  // The same path a human takes: paste the runtime token in Settings.
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

test("create a ticket and view it on its page", async ({ page }) => {
  const state = loadState();
  await connect(page, state.token);

  await page.goto("/");
  // Selecting the project waits for the projects fetch to land, so the
  // Create click below cannot outrun it.
  await page.getByLabel("In project").selectOption({ label: "Hero Project" });
  await page.getByLabel("Title").fill("E2E hero ticket");
  await page.getByRole("button", { name: "Create" }).click();

  const link = page.getByRole("link", { name: "E2E hero ticket" });
  await expect(link).toBeVisible();
  await link.click();

  await expect(page.getByRole("heading", { level: 1, name: "E2E hero ticket" })).toBeVisible();
  await expect(page.getByText("status: todo")).toBeVisible();
  await expect(page.getByLabel(/sync state/)).toBeVisible();
});

test("approve a seeded agent proposal from the Inbox", async ({ page }) => {
  const state = loadState();
  await connect(page, state.token);

  await page.goto("/inbox");
  const proposal = page.getByLabel(/^proposal /).filter({ hasText: "Seeded ticket" });
  await expect(proposal).toBeVisible();
  await expect(proposal.getByText("e2e seeded proposal")).toBeVisible();

  await proposal.getByRole("button", { name: "Approve" }).click();
  // E20-T6: approve shows a persistent success row with the resulting state + Undo.
  const approved = page.getByRole("status");
  await expect(approved).toContainText("Approved");
  await expect(approved.getByRole("button", { name: "Undo" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);

  // The approved change is applied: the ticket page shows the new status.
  await page.goto(`/tickets/${state.ticket_id}`);
  await expect(page.getByRole("heading", { level: 1, name: "Seeded ticket" })).toBeVisible();
  await expect(page.getByText("status: doing")).toBeVisible();
});
