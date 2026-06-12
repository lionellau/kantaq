/** E19-T2 — ticket page: fields, safe markdown, merged timeline, attachments. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { buildActivity, buildComment, buildProject, buildTicket } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer()
    .on("GET /v1/projects/{project_id}", buildProject())
    .on("GET /v1/tickets/{ticket_id}/comments", [])
    .on("GET /v1/tickets/{ticket_id}/activity", []);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the ticket page", () => {
  it("renders the header fields, rail badges, and attachments", async () => {
    server.on(
      "GET /v1/tickets/{ticket_id}",
      buildTicket({
        title: "Wire the inbox",
        status: "doing",
        priority: "high",
        labels: ["ui", "sprint-2"],
        acceptance_criteria: "queue renders; approve works",
        sync_state: "draft",
        pending_proposals: 1,
        attachments: [
          { blob_id: "b1", filename: "design.pdf", media_type: "application/pdf", size_bytes: 9 },
        ],
      }),
    );
    renderApp("/tickets/tick-1");

    expect(await screen.findByRole("heading", { level: 1, name: "Wire the inbox" })).toBeDefined();
    expect(screen.getByText("status: doing")).toBeDefined();
    expect(screen.getByText("priority: high")).toBeDefined();
    expect(screen.getByText("ui")).toBeDefined();
    // The project name arrives from its own fetch, one tick after the ticket.
    expect(await screen.findByText("project: Apollo")).toBeDefined();
    expect(screen.getByText("queue renders; approve works")).toBeDefined();
    expect(screen.getByLabelText("sync state: draft")).toBeDefined();
    expect(screen.getByLabelText("1 pending proposal")).toBeDefined();
    expect(screen.getByRole("button", { name: "design.pdf" })).toBeDefined();
  });

  it("renders markdown without executing or rendering raw HTML (PRD §15)", async () => {
    server.on(
      "GET /v1/tickets/{ticket_id}",
      buildTicket({
        description:
          "**bold move**\n\n<script>window.pwned = true</script><img src=x onerror=alert(1)>",
      }),
    );
    renderApp("/tickets/tick-1");

    const bold = await screen.findByText("bold move");
    expect(bold.tagName).toBe("STRONG");
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img[onerror]")).toBeNull();
    expect((window as { pwned?: boolean }).pwned).toBeUndefined();
  });

  it("merges comments and activity chronologically, skipping comment.create rows", async () => {
    server
      .on("GET /v1/tickets/{ticket_id}", buildTicket())
      .on("GET /v1/tickets/{ticket_id}/comments", [
        buildComment({ id: "c1", body: "first words", created_at: "2026-01-02T00:00:00" }),
      ])
      .on("GET /v1/tickets/{ticket_id}/activity", [
        buildActivity({
          id: "a1",
          action: "ticket.update",
          before: { status: "todo" },
          after: { status: "doing" },
          created_at: "2026-01-03T00:00:00",
        }),
        buildActivity({ id: "a2", action: "comment.create", created_at: "2026-01-02T00:00:00" }),
      ]);
    renderApp("/tickets/tick-1");

    expect(await screen.findByText("first words")).toBeDefined();
    expect(screen.getByText(/ticket\.update: status/)).toBeDefined();
    // The comment.create audit row is the comment itself — not shown twice.
    expect(screen.queryByText(/comment\.create/)).toBeNull();
    const items = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    const commentIndex = items.findIndex((text) => text.includes("first words"));
    const updateIndex = items.findIndex((text) => text.includes("ticket.update"));
    expect(commentIndex).toBeGreaterThanOrEqual(0);
    expect(commentIndex).toBeLessThan(updateIndex);
  });

  it("posts a comment from the composer", async () => {
    server.on("GET /v1/tickets/{ticket_id}", buildTicket());
    server.on(
      "POST /v1/tickets/{ticket_id}/comments",
      () => new Response(JSON.stringify(buildComment({ body: "on it" })), { status: 201 }),
    );
    renderApp("/tickets/tick-1");
    await screen.findByRole("heading", { level: 1, name: "Fix the flux capacitor" });

    fireEvent.change(screen.getByLabelText("Add a comment"), { target: { value: "on it" } });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "POST" && c.path === "/v1/tickets/tick-1/comments",
      );
      expect(call).toBeDefined();
    });
  });
});
