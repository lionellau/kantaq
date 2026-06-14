/** E21-T2 (SEC) — My Agent: own-loopback snippet, client-side token, fail closed. */

import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildSnippet } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";
import { isLoopback } from "./MyAgent";

let server: MockApiServer;

beforeEach(() => {
  setToken("kq_session.token");
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → My Agent", () => {
  it("renders the snippet with the member's own loopback URL and session token", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    renderApp("/settings/my-agent");

    const snippet = await screen.findByTestId("agent-snippet");
    // The placeholder is substituted client-side with the session token —
    // the server response itself never carried it.
    expect(snippet.textContent).toContain("Bearer kq_session.token");
    expect(snippet.textContent).not.toContain("KANTAQ_MEMBER_TOKEN");
    expect(snippet.textContent).toContain("http://127.0.0.1:54321/v1/mcp");
  });

  it("switches between the Claude Code and Cursor snippets (E11-T2)", async () => {
    server.on("GET /v1/me/agent-snippet", buildSnippet());
    renderApp("/settings/my-agent");

    // Defaults to Claude Code: the .mcp.json shape names the http transport.
    const snippet = await screen.findByTestId("agent-snippet");
    expect(snippet.textContent).toContain('"type": "http"');
    expect(screen.getByText(/Save this as/).textContent).toContain(".mcp.json");

    // Switching to Cursor renders the .cursor/mcp.json shape (bare url, no type).
    fireEvent.click(screen.getByRole("tab", { name: "Cursor" }));
    expect(screen.getByTestId("agent-snippet").textContent).not.toContain('"type"');
    expect(screen.getByText(/Save this as/).textContent).toContain(".cursor/mcp.json");
    // Either way the session token is substituted, never the placeholder.
    expect(screen.getByTestId("agent-snippet").textContent).toContain("Bearer kq_session.token");
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
    // And the secret was never rendered anywhere.
    expect(document.body.textContent).not.toContain("kq_session.token");
  });

  it("explains how to start the gateway when it is not running", async () => {
    server.on(
      "GET /v1/me/agent-snippet",
      buildSnippet({ gateway_live: false, gateway_url: null, snippet: null }),
    );
    renderApp("/settings/my-agent");

    expect(await screen.findByText("kantaq mcp dev")).toBeDefined();
    expect(screen.queryByTestId("agent-snippet")).toBeNull();
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
