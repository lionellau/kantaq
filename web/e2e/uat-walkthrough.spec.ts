/**
 * Sprint 1-7 UAT — Chrome walkthrough (Track B of docs/test/sprint-1-7-uat-plan.md).
 *
 * A guided tour of every delivered web surface, driven on headless Chromium the
 * same way a human does (paste the runtime token in Settings), against the real
 * seeded runtime that scripts/e2e_server.py boots on a throwaway local DB. Each
 * screen is asserted on a stable landmark and captured as a full-page screenshot
 * under e2e/.uat-shots/, which the results report (sprint-1-7-uat-results.md)
 * embeds. The story ends by exercising the two human-in-the-loop trust flows —
 * approve an agent proposal and resolve a sync conflict — capturing before/after.
 *
 * Run it in isolation so it gets a freshly-seeded runtime (the approve/resolve
 * mutate shared state). KANTAQ_UAT=1 lifts the default-run exclusion:
 *   KANTAQ_UAT=1 pnpm -C web exec playwright test e2e/uat-walkthrough.spec.ts
 *
 * The Settings → My Agent page is asserted but intentionally NOT screenshotted:
 * it renders a member bearer token in the connection snippet, and we never
 * persist credential-shaped strings (same rule as the v0.1 release dry-run).
 */

import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";

interface E2EState {
  base_url: string;
  token: string;
  ticket_id: string;
  project_id: string;
  conflict_id: string;
  conflict_ticket_id: string;
}

const SHOTS = path.resolve(process.env.KANTAQ_UAT_SHOTS ?? "e2e/.uat-shots");

function loadState(): E2EState {
  return JSON.parse(readFileSync(path.resolve("e2e/.runtime/state.json"), "utf-8")) as E2EState;
}

let seq = 0;
async function shot(page: Page, name: string): Promise<void> {
  seq += 1;
  const file = path.join(SHOTS, `${String(seq).padStart(2, "0")}-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
}

async function connect(page: Page, token: string): Promise<void> {
  await page.goto("/settings");
  await page.getByLabel(/runtime token/i).fill(token);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Connected to your local runtime.")).toBeVisible();
}

test("UAT walkthrough — every delivered screen, with screenshots", async ({ page }) => {
  mkdirSync(SHOTS, { recursive: true });
  const state = loadState();

  // --- B0 connect (the human path: paste the token in Settings) -------------
  await connect(page, state.token);
  await shot(page, "settings-connected");

  // --- B1 onboarding wizard --------------------------------------------------
  await page.goto("/onboarding");
  await expect(page.getByRole("heading", { level: 1, name: "Welcome to kantaq" })).toBeVisible();
  await shot(page, "onboarding");

  // --- B2 backlog: the seeded project + tickets are listed -------------------
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Backlog" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Seeded ticket" })).toBeVisible();
  await shot(page, "backlog");

  // --- B3 ticket page: header + body + trust rail ----------------------------
  await page.goto(`/tickets/${state.ticket_id}`);
  await expect(page.getByRole("heading", { level: 1, name: "Seeded ticket" })).toBeVisible();
  await shot(page, "ticket-page");

  // --- B4 inbox · proposals (BEFORE approve): diff + cited memory ------------
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  const proposal = page.getByLabel(/^proposal /).filter({ hasText: "Seeded ticket" });
  await expect(proposal).toBeVisible();
  await expect(proposal.getByText("e2e seeded proposal")).toBeVisible();
  await shot(page, "inbox-proposal-pending");

  // --- B5 inbox · sync conflicts: both candidate values ----------------------
  await page.getByRole("tab", { name: /Sync conflicts/ }).click();
  await expect(page.getByTestId("conflict-keep-a")).toHaveText("doing");
  await expect(page.getByTestId("conflict-keep-b")).toHaveText("todo");
  await shot(page, "inbox-conflict-open");

  // --- B6 inbox · denied calls (live tool.deny audit feed) -------------------
  // The seeded propose-only agent reached for the approve tool; the gateway
  // denied it (tool_allowlist) and the human sees that denial here (UAT-A6.1).
  await page.getByRole("tab", { name: /Denied/ }).click();
  await expect(page.getByText(/denied: tool_allowlist/)).toBeVisible();
  await shot(page, "inbox-denied-calls");

  // --- B7 memory graph -------------------------------------------------------
  await page.goto("/memory");
  await expect(page.getByRole("heading", { level: 1, name: "Memory" })).toBeVisible();
  await shot(page, "memory");

  // --- B8 agents (audit-driven trust surface) --------------------------------
  await page.goto("/agents");
  await expect(page.getByRole("heading", { level: 1, name: "Agents" })).toBeVisible();
  await shot(page, "agents");

  // --- B9 settings tree + every subpage --------------------------------------
  await page.goto("/settings");
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await shot(page, "settings-tree");

  const subpages: Array<[string, string]> = [
    ["workspace", "Workspace"],
    ["identity", "Identity"],
    ["members", "Members"],
    ["devices", "Devices"],
    ["sync", "Sync"],
    ["skill-mappings", "Skill mappings"],
    ["telemetry", "Telemetry"],
    ["export", "Export"],
  ];
  for (const [slug, heading] of subpages) {
    await page.goto(`/settings/${slug}`);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await shot(page, `settings-${slug}`);
  }

  // My Agent: assert it renders, but DO NOT screenshot (renders a bearer token).
  await page.goto("/settings/my-agent");
  await expect(page.getByRole("heading", { level: 1, name: "My Agent" })).toBeVisible();

  // --- B10 ACTION: a human approves the agent proposal -----------------------
  await page.goto("/inbox");
  await page
    .getByLabel(/^proposal /)
    .filter({ hasText: "Seeded ticket" })
    .getByRole("button", { name: "Approve" })
    .click();
  // E20-T6: approve shows a persistent success row (with the resulting state + Undo).
  await expect(page.getByRole("status")).toContainText("Approved");
  await shot(page, "inbox-proposal-approved");

  // the approved change is applied on the ticket (status todo → doing)
  await page.goto(`/tickets/${state.ticket_id}`);
  await expect(page.getByText("status: doing")).toBeVisible();
  await shot(page, "ticket-after-approval");

  // --- B11 ACTION: a human resolves the sync conflict ------------------------
  await page.goto("/inbox");
  await page.getByRole("tab", { name: /Sync conflicts/ }).click();
  await page.getByRole("button", { name: "Keep A" }).click();
  await expect(page.getByText(/Resolved —/)).toBeVisible();
  await expect(page.getByText(/No sync conflicts/)).toBeVisible();
  await shot(page, "inbox-conflict-resolved");
});
