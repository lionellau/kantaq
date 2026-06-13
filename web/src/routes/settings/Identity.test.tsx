/** E20-T2 (MOD-12) — Settings → Identity: the member, their scopes, and grants. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildGrant, buildMe } from "../../test/builders";
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

describe("Settings → Identity", () => {
  it("shows the member identity and their capability grants", async () => {
    server
      .on("GET /v1/me", buildMe({ email: "ada@example.com", role: "Maintainer" }))
      .on("GET /v1/grants", [buildGrant({ resource: "workspace/main", verbs: ["tickets.read"] })]);
    renderApp("/settings/identity");

    expect(await screen.findByText("ada@example.com")).toBeDefined();
    expect(screen.getByText("Maintainer")).toBeDefined();
    expect(screen.getByText("workspace/main")).toBeDefined();
    expect(screen.getByText("tickets.read")).toBeDefined();
    expect(screen.getByRole("button", { name: "Revoke" })).toBeDefined();
  });

  it("notes that a human token carries no scopes", async () => {
    server.on("GET /v1/me", buildMe({ scopes: [] })).on("GET /v1/grants", []);
    renderApp("/settings/identity");
    expect(await screen.findByText(/your role decides/)).toBeDefined();
    expect(screen.getByText("No grants issued.")).toBeDefined();
  });

  it("renders an agent token's scopes as chips", async () => {
    server
      .on("GET /v1/me", buildMe({ role: "Agent", scopes: ["tickets.read", "proposals.write"] }))
      .on("GET /v1/grants", []);
    renderApp("/settings/identity");
    expect(await screen.findByText("proposals.write")).toBeDefined();
  });

  it("revokes a grant through the MOD-06 path", async () => {
    server
      .on("GET /v1/me", buildMe())
      .on("GET /v1/grants", [buildGrant({ id: "grant-7" })])
      .on("POST /v1/grants/{grant_id}/revoke", buildGrant({ id: "grant-7", valid: false }));
    renderApp("/settings/identity");

    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "POST" && c.path === "/v1/grants/grant-7/revoke",
      );
      expect(call).toBeDefined();
    });
  });
});
