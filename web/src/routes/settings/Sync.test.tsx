/** E20-T2 (MOD-12) — Settings → Sync: honest local-first status, no fake action. */

import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildSyncStatus, buildWorkspaceMetrics } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → Sync", () => {
  it("shows local-only mode and the event-log counts", async () => {
    server.on(
      "GET /v1/sync/status",
      buildSyncStatus({ hub_mode: "local", pending_events: 3, total_events: 3 }),
    );
    renderApp("/settings/sync");

    expect(await screen.findByText(/Local only/)).toBeDefined();
    expect(screen.getByText(/No remote backend configured/)).toBeDefined();
    expect(screen.getByTestId("sync-pending").textContent).toBe("3");
  });

  it("reports a configured supabase backend", async () => {
    server.on(
      "GET /v1/sync/status",
      buildSyncStatus({ hub_mode: "supabase", backend_configured: true, committed_events: 5 }),
    );
    renderApp("/settings/sync");

    expect(await screen.findByText(/Supabase/)).toBeDefined();
    expect(screen.getByText(/A remote backend is configured/)).toBeDefined();
  });

  it("surfaces the agent-proposal staleness policy (read-only)", async () => {
    server.on(
      "GET /v1/sync/status",
      buildSyncStatus({ agent_proposal_stale_policy: "strict_rebase" }),
    );
    renderApp("/settings/sync");

    expect((await screen.findByTestId("proposal-stale-policy")).textContent).toMatch(/Strict/);
  });

  it("guards when not connected", () => {
    clearToken();
    renderApp("/settings/sync");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });
});

describe("Settings → Sync: the metrics dashboard (E20-T5 / MOD-27)", () => {
  beforeEach(() => {
    server.on("GET /v1/sync/status", buildSyncStatus({ hub_mode: "supabase" }));
  });

  it("renders the capacity gauge and the Supabase billing deep-link", async () => {
    server.on("GET /v1/metrics/summary", buildWorkspaceMetrics());
    renderApp("/settings/sync");

    expect(await screen.findByTestId("metrics-dashboard")).toBeDefined();
    expect(screen.getByTestId("capacity-pct").textContent).toBe("24.0%");
    const billing = screen.getByRole("link", { name: /View billing in Supabase/ });
    expect(billing.getAttribute("href")).toMatch(/supabase\.com\/dashboard/);
  });

  it("warns when the free tier is about to bite", async () => {
    const base = buildWorkspaceMetrics();
    const backend = base.backend;
    if (backend === null) {
      throw new Error("builder should provide a backend");
    }
    server.on("GET /v1/metrics/summary", {
      ...base,
      backend: {
        ...backend,
        capacity: { ...backend.capacity, db_pct: 0.92, headroom_warning: true },
      },
    });
    renderApp("/settings/sync");
    expect(await screen.findByTestId("headroom-warning")).toBeDefined();
  });

  it("warns when an idle free-tier project risks being paused", async () => {
    const base = buildWorkspaceMetrics();
    const backend = base.backend;
    if (backend === null) {
      throw new Error("builder should provide a backend");
    }
    server.on("GET /v1/metrics/summary", {
      ...base,
      backend: {
        ...backend,
        capacity: { ...backend.capacity, idle_pause_risk: true },
      },
    });
    renderApp("/settings/sync");
    expect(await screen.findByTestId("idle-pause-warning")).toBeDefined();
  });

  it("shows the per-actor agent activity table", async () => {
    server.on("GET /v1/metrics/summary", buildWorkspaceMetrics());
    renderApp("/settings/sync");

    expect(await screen.findByText(/Agent activity/)).toBeDefined();
    expect(screen.getByText("agent-1")).toBeDefined();
    expect(screen.getByRole("columnheader", { name: "~Tokens" })).toBeDefined();
  });

  it("explains a local-only workspace has no backend capacity", async () => {
    server.on(
      "GET /v1/metrics/summary",
      buildWorkspaceMetrics({ hub_mode: "local", backend: null }),
    );
    renderApp("/settings/sync");
    expect(await screen.findByText(/Local-only workspace/)).toBeDefined();
  });
});
