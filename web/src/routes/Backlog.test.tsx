/** E19-T1 — backlog list: rows, badges, filters drive the query, create posts. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { buildMember, buildProject, buildTicket } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer()
    .on("GET /v1/projects", [buildProject()])
    .on("GET /v1/members", [buildMember()]);
});

afterEach(() => {
  server.restore();
  clearToken();
});

function ticketCalls(): string[] {
  return server.calls
    .filter((call) => call.method === "GET" && call.path === "/v1/tickets")
    .map((call) => new URL(call.request.url).search);
}

describe("the backlog list", () => {
  it("renders rows with sync badges and proposal chips", async () => {
    server.on("GET /v1/tickets", [
      buildTicket({
        id: "tick-1",
        title: "Draft ticket",
        sync_state: "draft",
        pending_proposals: 2,
        assignee: "member-1",
      }),
      buildTicket({ id: "tick-2", title: "Synced ticket", sync_state: "committed" }),
    ]);
    renderApp("/");

    expect(await screen.findByRole("link", { name: "Draft ticket" })).toBeDefined();
    expect(screen.getByRole("link", { name: "Synced ticket" })).toBeDefined();
    expect(screen.getByLabelText("sync state: draft")).toBeDefined();
    expect(screen.getByLabelText("sync state: committed")).toBeDefined();
    expect(screen.getByLabelText("2 pending proposals")).toBeDefined();
    // The assignee renders as the member's email, the project as its name
    // (each also appears in its filter select, so scope to the row).
    const row = screen.getByRole("link", { name: "Draft ticket" }).closest("tr");
    expect(row?.textContent).toContain("owner@example.com");
    expect(row?.textContent).toContain("Apollo");
    // Rows link to the ticket page.
    expect(screen.getByRole("link", { name: "Draft ticket" }).getAttribute("href")).toBe(
      "/tickets/tick-1",
    );
  });

  it("sends each filter as its query parameter (FR-E19-1)", async () => {
    server.on("GET /v1/tickets", []);
    renderApp("/");
    await screen.findByText("No tickets match.");

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "doing" } });
    await waitFor(() => {
      expect(ticketCalls().at(-1)).toBe("?status=doing");
    });

    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "proj-1" } });
    await waitFor(() => {
      expect(ticketCalls().at(-1)).toBe("?project=proj-1&status=doing");
    });

    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "bug" } });
    await waitFor(() => {
      expect(ticketCalls().at(-1)).toContain("label=bug");
    });
  });

  it("creates a ticket from the inline form", async () => {
    server.on("GET /v1/tickets", []);
    let posted = false;
    server.on("POST /v1/tickets", () => {
      posted = true; // body asserted below via the recorded call
      return new Response(JSON.stringify(buildTicket()), { status: 201 });
    });
    renderApp("/");
    await screen.findByText("No tickets match.");

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Ship the inbox" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(posted).toBe(true);
    });
    const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/tickets");
    expect(call).toBeDefined();
    const body = (await call?.request.json()) as { title: string; project_id: string };
    expect(body.title).toBe("Ship the inbox");
    expect(body.project_id).toBe("proj-1");
  });

  it("asks to connect when there is no session, and shows how to get the token", () => {
    clearToken();
    renderApp("/");
    expect(screen.getByText(/Not connected/)).toBeDefined();
    // DEBT-34: the disconnected screen surfaces the literal command + a copy button.
    expect(screen.getByText("kantaq token show")).toBeDefined();
    expect(screen.getByRole("button", { name: "Copy" })).toBeDefined();
    expect(ticketCalls()).toHaveLength(0);
  });
});
