/**
 * Sprint 1-7 UAT — Track B, two-user concurrent collaboration.
 *
 * Everything else drives one user; this drives TWO at once (two browser
 * contexts: User A = Owner, User B = Member) against the same runtime, to prove
 * the multi-user behaviour the product actually supports:
 *
 *  1. Concurrent comments on the same ticket propagate live (the 2 s poll) — A
 *     posts, B sees it without reloading, and vice-versa. (There is no direct
 *     field-edit UI: human writes are comments or the propose→approve flow, so
 *     "concurrent edits" = concurrent comments, which are append-only and never
 *     conflict. Hard divergent-write conflicts are the cross-replica SYNC path,
 *     seeded + proven in conflict-flow.spec.ts.)
 *  2. A shared sync conflict: both users see it; when A resolves it, it leaves
 *     B's queue on B's next poll — collaborative, resolved exactly once.
 *
 * Run in isolation (KANTAQ_UAT=1 lifts the default-run exclusion):
 *   KANTAQ_UAT=1 pnpm -C web exec playwright test e2e/uat-collab.spec.ts
 */

import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

interface E2EState {
  token: string;
  ticket_id: string;
  conflict_ticket_id: string;
  role_tokens: Record<string, string>;
}

const SHOTS = path.resolve(process.env.KANTAQ_UAT_SHOTS ?? "e2e/.uat-shots");

function loadState(): E2EState {
  return JSON.parse(readFileSync(path.resolve("e2e/.runtime/state.json"), "utf-8")) as E2EState;
}

async function connect(page: Page, token: string): Promise<void> {
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));

test("two users on one ticket: comments propagate live (2s poll)", async ({ browser }) => {
  const state = loadState();
  const ctxA = await browser.newContext();
  const ctxB = await browser.newContext();
  const a = await ctxA.newPage(); // Owner
  const b = await ctxB.newPage(); // Member

  await connect(a, state.token);
  await connect(b, state.role_tokens.Member);
  await a.goto(`/tickets/${state.ticket_id}`);
  await b.goto(`/tickets/${state.ticket_id}`);

  // A posts → B sees it WITHOUT reloading (B's 2s poll re-fetches comments).
  await a.getByLabel("Add a comment").fill("comment from the Owner");
  await a.getByRole("button", { name: "Comment" }).click();
  await expect(b.getByText("comment from the Owner")).toBeVisible({ timeout: 10_000 });

  // …and back the other way: B posts → A sees it live.
  await b.getByLabel("Add a comment").fill("reply from the Member");
  await b.getByRole("button", { name: "Comment" }).click();
  await expect(a.getByText("reply from the Member")).toBeVisible({ timeout: 10_000 });

  await a.screenshot({ path: path.join(SHOTS, "collab-01-userA-sees-both.png"), fullPage: true });
  await ctxA.close();
  await ctxB.close();
});

test("two users + a sync conflict: A resolves, it clears from B's queue", async ({ browser }) => {
  const state = loadState();
  const ctxA = await browser.newContext();
  const ctxB = await browser.newContext();
  const a = await ctxA.newPage(); // Owner — resolves
  const b = await ctxB.newPage(); // Member — observes

  await connect(a, state.token);
  await connect(b, state.role_tokens.Member);

  // Both open the shared conflict.
  for (const p of [a, b]) {
    await p.goto("/inbox");
    await p.getByRole("tab", { name: /Sync conflicts/ }).click();
  }
  await expect(a.getByTestId("conflict-keep-a")).toBeVisible();
  await expect(b.getByTestId("conflict-keep-a")).toBeVisible();
  await b.screenshot({
    path: path.join(SHOTS, "collab-02-userB-conflict-open.png"),
    fullPage: true,
  });

  // A resolves → B's queue clears on B's next poll, with no action by B.
  await a.getByRole("button", { name: "Keep A" }).click();
  await expect(a.getByText(/Resolved —/)).toBeVisible();
  await expect(b.getByText(/No sync conflicts/)).toBeVisible({ timeout: 10_000 });

  await b.screenshot({
    path: path.join(SHOTS, "collab-03-userB-conflict-cleared.png"),
    fullPage: true,
  });
  await ctxA.close();
  await ctxB.close();
});
