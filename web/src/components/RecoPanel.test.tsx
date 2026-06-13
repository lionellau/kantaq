/** E17-T2 (MOD-22) — the ticket recommendation panel. */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildRecommendation } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import RecoPanel from "./RecoPanel";

let server: MockApiServer;

beforeEach(() => {
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  vi.restoreAllMocks();
});

describe("the recommendation panel", () => {
  it("renders each recommendation's structured contract", async () => {
    server.on("GET /v1/tickets/{ticket_id}/recommendations", [
      buildRecommendation({
        role: "code_agent",
        skill_container: "security-review",
        why: "At the Review stage, kantaq recommends a code_agent run Security review.",
        risk_level: "high",
        confidence: "rule_match_strong",
        approval_rule: "read_only",
        expected_output: "a security review with findings",
        missing_memory: ["codebase", "decision"],
      }),
    ]);
    render(<RecoPanel ticketId="tick-1" />);

    expect(await screen.findByText("Security review")).toBeDefined();
    expect(screen.getByText("code_agent")).toBeDefined();
    expect(screen.getByText("high risk")).toBeDefined();
    expect(screen.getByText("strong match")).toBeDefined();
    expect(screen.getByText("read-only")).toBeDefined();
    expect(screen.getByText("a security review with findings")).toBeDefined();
    // The resolver's missing-memory signal is surfaced.
    expect(screen.getByText(/Missing context: codebase, decision/)).toBeDefined();
  });

  it("copies the MCP session snippet to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    server.on("GET /v1/tickets/{ticket_id}/recommendations", [
      buildRecommendation({ mcp_session_template: 'role_context_get(ticket="tick-1")' }),
    ]);
    render(<RecoPanel ticketId="tick-1" />);

    const button = await screen.findByRole("button", { name: "Copy MCP snippet" });
    fireEvent.click(button);

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('role_context_get(ticket="tick-1")'),
    );
    expect(await screen.findByRole("button", { name: "Copied ✓" })).toBeDefined();
  });

  it("shows an empty state when nothing is recommended", async () => {
    server.on("GET /v1/tickets/{ticket_id}/recommendations", []);
    render(<RecoPanel ticketId="tick-1" />);
    expect(await screen.findByText("No recommendations for this stage.")).toBeDefined();
  });

  it("shows an error state when the request fails", async () => {
    server.on(
      "GET /v1/tickets/{ticket_id}/recommendations",
      () => new Response("nope", { status: 500 }),
    );
    render(<RecoPanel ticketId="tick-1" />);
    expect(await screen.findByText("Could not load recommendations.")).toBeDefined();
  });
});
