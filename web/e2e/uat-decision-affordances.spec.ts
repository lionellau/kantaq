/**
 * E20-T6 (MOD-12) — identity humanization + the Approve Undo, end to end.
 *
 * Isolated (KANTAQ_UAT=1, own server boot) because it consumes the single
 * seeded proposal: the default-run hero-flow owns that proposal's approve path,
 * so the Undo-revert (which leaves the ticket back at `todo`) must not race it.
 * The default gate proves approve→success-row+Undo (hero-flow); this proves the
 * Undo actually reverts, and that the proposer reads as a person, not a ULID.
 *
 * Run in isolation:
 *   KANTAQ_UAT=1 pnpm -C web exec playwright test e2e/uat-decision-affordances.spec.ts
 */

import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

const SHOTS = path.resolve(process.env.KANTAQ_UAT_SHOTS ?? "e2e/.uat-shots");

function loadState(): { token: string; ticket_id: string } {
  return JSON.parse(readFileSync(path.resolve("e2e/.runtime/state.json"), "utf-8")) as {
    token: string;
    ticket_id: string;
  };
}

async function connect(page: Page, token: string): Promise<void> {
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));

test("the proposer reads as a name; Approve offers an Undo that reverts the ticket", async ({
  page,
}) => {
  const state = loadState();
  await connect(page, state.token);

  await page.goto("/inbox");
  const proposal = page.getByLabel(/^proposal /).filter({ hasText: "Seeded ticket" });
  await expect(proposal).toBeVisible();

  // Identity humanization: the proposer is the seeded agent (agent@e2e.local),
  // shown by name, not the raw actor ULID.
  await expect(proposal).toContainText("agent@e2e.local");
  await page.screenshot({ path: path.join(SHOTS, "decision-01-humanized.png"), fullPage: true });

  // Approve → a persistent success row surfaces the resulting state + Undo.
  await proposal.getByRole("button", { name: "Approve" }).click();
  const approved = page.getByRole("status");
  await expect(approved).toContainText("Approved");
  await expect(approved).toContainText("status");

  // Undo immediately (the success row is in-memory; it is the right-after-approve
  // affordance) → the ticket reverts to its captured pre-approve value.
  await approved.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByText(/Undone/)).toBeVisible();
  await page.screenshot({ path: path.join(SHOTS, "decision-02-undone.png"), fullPage: true });

  await page.goto(`/tickets/${state.ticket_id}`);
  await expect(page.getByText("status: todo")).toBeVisible();
});
