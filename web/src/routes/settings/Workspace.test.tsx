/** E20-T2 (MOD-12) — Settings → Workspace: name + id from /v1/me, admin links. */

import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildMe } from "../../test/builders";
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

describe("Settings → Workspace", () => {
  it("shows the workspace name and id and links to admin pages", async () => {
    server.on("GET /v1/me", buildMe({ workspace_name: "Acme Workspace", workspace_id: "ws-99" }));
    renderApp("/settings/workspace");

    expect(await screen.findByText("Acme Workspace")).toBeDefined();
    expect(screen.getByText("ws-99")).toBeDefined();
    expect(screen.getByRole("link", { name: "Members" })).toBeDefined();
    expect(screen.getByRole("link", { name: "Telemetry" })).toBeDefined();
  });

  it("guards when not connected", () => {
    clearToken();
    renderApp("/settings/workspace");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });
});
