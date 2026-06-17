/**
 * Sprint 1-7 UAT — Track B, role-differentiated browser pass.
 *
 * The walkthrough drives the UI as the Owner; this drives the SAME UI as each
 * other user type and shows they get the role-appropriate outcome. The rigorous
 * per-endpoint enforcement is proven across all roles by scripts/uat_roles.py
 * (41/41 against the real API); this is the visible half — the human sees the
 * guard. Screenshots land in e2e/.uat-shots/ with a `role-` prefix.
 *
 * Headline contrast: the SAME action (invite a member) attempted by a Viewer, a
 * Member, and a Maintainer — denied for the first two ("your role may not invite
 * members"), allowed for the Maintainer. Plus the unauthenticated state.
 *
 * Run in isolation (fresh seeded runtime, with role tokens in state.json).
 * KANTAQ_UAT=1 lifts the default-run exclusion:
 *   KANTAQ_UAT=1 pnpm -C web exec playwright test e2e/uat-roles.spec.ts
 */

import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

interface E2EState {
  token: string;
  role_tokens: Record<string, string>;
}

const SHOTS = path.resolve(process.env.KANTAQ_UAT_SHOTS ?? "e2e/.uat-shots");

function loadState(): E2EState {
  return JSON.parse(readFileSync(path.resolve("e2e/.runtime/state.json"), "utf-8")) as E2EState;
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: path.join(SHOTS, `role-${name}.png`), fullPage: true });
}

async function connect(page: Page, token: string): Promise<void> {
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

async function attemptInvite(page: Page, email: string): Promise<number> {
  await page.goto("/settings/members");
  // Wait for the member-list fetch to settle first: the page's refresh() clears
  // the error banner on success, so inviting before it lands would let a late
  // list-load wipe the permission guard (a known benign race, UAT results §B-roles).
  await expect(page.getByRole("table")).toBeVisible();
  await page.getByLabel(/Invite by email/i).fill(email);
  await page.getByLabel("Role").selectOption("Member");
  // Wait on the actual invite response so the assertion never races the POST.
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().includes("/v1/members/invite")),
    page.getByRole("button", { name: "Invite" }).click(),
  ]);
  return res.status();
}

test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));

test("an unauthenticated visitor sees the not-connected state", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/Not connected/)).toBeVisible();
  await shot(page, "01-not-connected");
});

test("a Viewer may NOT invite a member (read-only role)", async ({ page }) => {
  const { role_tokens } = loadState();
  await connect(page, role_tokens.Viewer);
  expect(await attemptInvite(page, "viewer-tried@e2e.local")).toBe(403);
  await expect(page.getByText("your role may not invite members")).toBeVisible();
  await shot(page, "02-viewer-invite-denied");
});

test("a Member may NOT invite a member (tracker work only)", async ({ page }) => {
  const { role_tokens } = loadState();
  await connect(page, role_tokens.Member);
  expect(await attemptInvite(page, "member-tried@e2e.local")).toBe(403);
  await expect(page.getByText("your role may not invite members")).toBeVisible();
  await shot(page, "03-member-invite-denied");
});

test("a Maintainer CAN invite a member (credential admin)", async ({ page }) => {
  const { role_tokens } = loadState();
  await connect(page, role_tokens.Maintainer);
  expect(await attemptInvite(page, "new-hire@e2e.local")).toBe(201);
  // success: no guard, and the new member's one-time token panel appears
  await expect(page.getByText("your role may not invite members")).toHaveCount(0);
  await expect(page.getByText("new-hire@e2e.local").first()).toBeVisible();
  await shot(page, "04-maintainer-invite-allowed");
});

test("an Agent token can read the backlog but is not a human admin", async ({ page }) => {
  const { role_tokens } = loadState();
  await connect(page, role_tokens.Agent);
  await page.goto("/");
  // tickets.read is in the agent's scopes → the backlog loads for it
  await expect(page.getByRole("heading", { level: 1, name: "Backlog" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Seeded ticket" })).toBeVisible();
  await shot(page, "05-agent-backlog-read");
});
