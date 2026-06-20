/**
 * E21-T2 + E20-T7 (SEC) — My Agent: the snippet defaults to a scoped Agent
 * token (never the owner's), the owner token stays an explicit opt-in, the
 * gateway auto-detects (no manual Reload), and the connection badge is honest.
 */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildAuditCall, buildMember, buildSnippet } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";
import { isLoopback } from "./MyAgent";

let server: MockApiServer;

// What POST /v1/members/invite returns for the scoped Agent (token shown once).
const INVITE_AGENT = {
  member: buildMember({ id: "ag-1", email: "my-coding-agent@agents.local", role: "Agent" }),
  token: "scoped-agent-token",
};

function recentIso(): string {
  return new Date(Date.now() - 60_000).toISOString();
}

beforeEach(() => {
  setToken("kq_session.token");
  // The badge polls the most-recent mcp audit call; default to none.
  server = new MockApiServer().on("GET /v1/audit/range", []);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → My Agent: the scoped-token default (E20-T7, SEC)", () => {
  it("defaults to a scoped Agent token and never embeds the owner's own token", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    renderApp("/settings/my-agent");

    // Before any action the owner token is NOT embedded — the default is scoped,
    // so the snippet still carries the placeholder, never the session token.
    const snippet = await screen.findByTestId("agent-snippet");
    expect(snippet.textContent).not.toContain("kq_session.token");
    expect(snippet.textContent).toContain("KANTAQ_MEMBER_TOKEN");

    // Mint the scoped agent.
    server.on("POST /v1/members/invite", INVITE_AGENT);
    fireEvent.click(screen.getByRole("button", { name: "Create scoped agent token" }));

    // The snippet now carries the SCOPED agent token, never the owner token.
    await waitFor(() =>
      expect(screen.getByTestId("agent-snippet").textContent).toContain("scoped-agent-token"),
    );
    expect(screen.getByTestId("agent-snippet").textContent).not.toContain("kq_session.token");
    // The embedded identity is labelled.
    expect(screen.getByText(/Embedded identity/).textContent).toContain(
      "my-coding-agent@agents.local",
    );
    expect(screen.getByText(/propose-only/)).toBeDefined();

    // Contract: the invite carried the Agent role + the propose-first scopes —
    // a real propose-only grant, not the owner's reach (D-03).
    const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/members/invite");
    const body = (await call?.request.json()) as { role: string; scopes: string[] };
    expect(body.role).toBe("Agent");
    expect(body.scopes).toEqual(["tickets.read", "proposals.write"]);
  });

  it("the owner-token opt-in embeds the member's own token (existing setups keep working)", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    renderApp("/settings/my-agent");
    await screen.findByTestId("agent-snippet");

    fireEvent.click(screen.getByRole("button", { name: /use your own member token/i }));

    await waitFor(() =>
      expect(screen.getByTestId("agent-snippet").textContent).toContain("Bearer kq_session.token"),
    );
  });

  it("switches between the Claude Code, Cursor, and Codex snippets (E11)", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    server.on("POST /v1/members/invite", INVITE_AGENT);
    renderApp("/settings/my-agent");
    await screen.findByTestId("agent-snippet");
    fireEvent.click(screen.getByRole("button", { name: "Create scoped agent token" }));
    await waitFor(() =>
      expect(screen.getByTestId("agent-snippet").textContent).toContain("scoped-agent-token"),
    );

    // Defaults to Claude Code: the .mcp.json shape names the http transport.
    expect(screen.getByTestId("agent-snippet").textContent).toContain('"type": "http"');
    expect(screen.getByText(/Save this as/).textContent).toContain(".mcp.json");

    // Cursor renders the .cursor/mcp.json shape (bare url, no type).
    fireEvent.click(screen.getByRole("tab", { name: "Cursor" }));
    expect(screen.getByTestId("agent-snippet").textContent).not.toContain('"type"');
    expect(screen.getByText(/Save this as/).textContent).toContain(".cursor/mcp.json");

    // Codex renders the TOML table + the env-var export (token out of the file).
    fireEvent.click(screen.getByRole("tab", { name: "Codex" }));
    const codex = screen.getByTestId("agent-snippet");
    expect(codex.textContent).toContain("[mcp_servers.kantaq]");
    expect(codex.textContent).toContain('bearer_token_env_var = "KANTAQ_AGENT_TOKEN"');
    expect(codex.textContent).not.toContain("scoped-agent-token"); // never in the file
    expect(screen.getByTestId("agent-setup").textContent).toContain(
      "export KANTAQ_AGENT_TOKEN=scoped-agent-token",
    );
  });

  it("refuses to render a non-loopback gateway URL (FR-E21-3)", async () => {
    server.on(
      "GET /v1/me/agent-snippet",
      buildSnippet({ gateway_url: "http://192.168.1.20:54321/v1/mcp" }),
    );
    renderApp("/settings/my-agent");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("not loopback");
    expect(screen.queryByTestId("agent-snippet")).toBeNull();
    expect(document.body.textContent).not.toContain("kq_session.token");
  });
});

describe("Settings → My Agent: gateway auto-detect + connection badge (E20-T7)", () => {
  it("prompts `kantaq mcp dev` and auto-detects (no manual Reload) when the gateway is down", async () => {
    server.on(
      "GET /v1/me/agent-snippet",
      buildSnippet({ gateway_live: false, gateway_url: null, snippet: null }),
    );
    renderApp("/settings/my-agent");

    expect(await screen.findByText("kantaq mcp dev")).toBeDefined();
    expect(screen.queryByTestId("agent-snippet")).toBeNull();
    expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
    expect(screen.getByText(/no need to reload/i)).toBeDefined();
  });

  it("the badge says offline when the gateway is down — never an optimistic green", async () => {
    server.on(
      "GET /v1/me/agent-snippet",
      buildSnippet({ gateway_live: false, gateway_url: null, snippet: null }),
    );
    // Even with an old call on record, a down gateway is offline.
    server.on("GET /v1/audit/range", [buildAuditCall()]);
    renderApp("/settings/my-agent");

    expect(await screen.findByLabelText(/agent connection: Gateway offline/)).toBeDefined();
  });

  it("the badge says active on a recent, successful audited mcp call", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    // A successful call has no `reason` (only denials do).
    server.on("GET /v1/audit/range", [
      buildAuditCall({ action: "agent.read", reason: null, detail: null, created_at: recentIso() }),
    ]);
    renderApp("/settings/my-agent");

    expect(await screen.findByLabelText(/agent connection: Active/)).toBeDefined();
  });

  it("a recent *denied* call never greens the badge — denials are activity, not health", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    // buildAuditCall defaults to a tool.deny row (it carries a reason).
    server.on("GET /v1/audit/range", [buildAuditCall({ created_at: recentIso() })]);
    renderApp("/settings/my-agent");

    // Gateway up + only denied activity → honest neutral, never an optimistic green.
    expect(await screen.findByLabelText(/no agent calls yet/)).toBeDefined();
    expect(screen.queryByLabelText(/agent connection: Active/)).toBeNull();
  });
});

describe("isLoopback", () => {
  it.each([
    ["http://127.0.0.1:1/v1/mcp", true],
    ["http://localhost:3939/v1/mcp", true],
    ["http://[::1]:3939/v1/mcp", true],
    ["http://192.168.1.20:3939/v1/mcp", false],
    ["http://evil.example/v1/mcp", false],
    ["not a url", false],
  ])("%s → %s", (url, expected) => {
    expect(isLoopback(url)).toBe(expected);
  });
});
