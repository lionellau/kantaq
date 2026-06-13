/**
 * E20-T3 — the Agents page: live sessions + denied calls, revoke, rotate.
 * The trust surface must be honest and complete (NFR-E20-1).
 */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { buildAgentSession, buildAuditCall, buildGrant } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer()
    .on("GET /v1/agents/sessions", [buildAgentSession()])
    .on("GET /v1/audit/range", []);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the Agents page", () => {
  it("requires a connection", () => {
    clearToken();
    renderApp("/agents");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });

  it("lists a session with owner, scope, tools, write mode and expiry", async () => {
    renderApp("/agents");

    expect(await screen.findByText("bot@example.com")).toBeDefined();
    expect(screen.getByText("workspace/main")).toBeDefined();
    expect(screen.getByText("proposals.write")).toBeDefined();
    expect(screen.getByText("tickets.read")).toBeDefined();
    expect(screen.getByText("propose-only")).toBeDefined();
    expect(screen.getByText("active")).toBeDefined();
  });

  it("shows each session's recent + denied calls from audit", async () => {
    server.on("GET /v1/audit/range", [
      buildAuditCall({
        actor_id: "agent-1",
        object_ref: "tools/memory_get",
        reason: "memory_policy",
      }),
    ]);
    renderApp("/agents");

    expect(await screen.findByText("memory_get")).toBeDefined();
    expect(screen.getByText(/denied: memory_policy/)).toBeDefined();
  });

  it("revokes a grant and reports it", async () => {
    server.on("POST /v1/grants/{grant_id}/revoke", () =>
      buildGrant({ valid: false, reason: "revoked" }),
    );
    renderApp("/agents");
    await screen.findByText("bot@example.com");

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => expect(screen.getByText(/Grant revoked/)).toBeDefined());
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/grants/grant-1/revoke",
    );
    expect(call).toBeDefined();
  });

  it("rotates a token and shows the new secret once", async () => {
    server.on("POST /v1/members/{member_id}/rotate", () => ({
      member_id: "agent-1",
      token: "kq_freshsecret",
    }));
    renderApp("/agents");
    await screen.findByText("bot@example.com");

    fireEvent.click(screen.getByRole("button", { name: "Rotate token" }));

    const minted = await screen.findByTestId("minted-token");
    expect(minted.textContent).toContain("kq_freshsecret");
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/members/agent-1/rotate",
    );
    expect(call).toBeDefined();
  });

  it("marks a revoked session inactive and disables Revoke", async () => {
    server.on("GET /v1/agents/sessions", [
      buildAgentSession({ active: false, reason: "revoked", revoked_at: "2026-01-01T00:00:00" }),
    ]);
    renderApp("/agents");

    const card = await screen.findByLabelText(/agent session grant-1/);
    expect(within(card).getByText("revoked")).toBeDefined();
    expect(screen.getByRole("button", { name: "Revoke" })).toHaveProperty("disabled", true);
  });

  it("shows the empty state when there are no agent sessions", async () => {
    server.on("GET /v1/agents/sessions", []);
    renderApp("/agents");
    expect(await screen.findByText(/No agent sessions/)).toBeDefined();
  });
});
