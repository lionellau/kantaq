/** E20-T2 (MOD-12) — Settings → Sync: honest local-first status, no fake action. */

import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildSyncStatus } from "../../test/builders";
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

  it("guards when not connected", () => {
    clearToken();
    renderApp("/settings/sync");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });
});
