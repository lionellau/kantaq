/**
 * E20-T1/T3/T4 — the Inbox: tabs, the proposal diff + cited memory, approve and
 * reject flows, the 409 race, the denied-calls tab, and Inbox-zero.
 */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Proposal } from "../api/types";
import { clearToken, setToken } from "../lib/session";
import {
  buildAuditCall,
  buildConflict,
  buildLinkedMemory,
  buildMemoryEntry,
  buildProposal,
  buildTicket,
} from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;
let queue: Proposal[];

beforeEach(() => {
  setToken("test-token");
  queue = [buildProposal()];
  server = new MockApiServer()
    .on("GET /v1/proposals", () => queue)
    .on("GET /v1/audit/range", [])
    // The proposal's ticket (for before-values) and its cited memory.
    .on("GET /v1/tickets/{ticket_id}", buildTicket({ status: "todo" }))
    .on("GET /v1/tickets/{ticket_id}/memory", []);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the Inbox queue", () => {
  it("renders a proposal as a before→after diff with the note", async () => {
    renderApp("/inbox");

    expect(await screen.findByRole("link", { name: "Fix the flux capacitor" })).toBeDefined();
    expect(screen.getByText(/proposed by agent-1/)).toBeDefined();
    // Field-level diff: the live ticket value (todo) struck, the proposed value (doing).
    expect(screen.getByText("status")).toBeDefined();
    expect(screen.getByText("todo")).toBeDefined();
    expect(screen.getByText("doing")).toBeDefined();
    expect(screen.getByText(/note: ready to start/)).toBeDefined();
  });

  it("shows the memory cited for the proposal's ticket", async () => {
    server.on("GET /v1/tickets/{ticket_id}/memory", [
      buildLinkedMemory({ entry: buildMemoryEntry({ id: "mem-9", title: "Cited design note" }) }),
    ]);
    renderApp("/inbox");

    expect(await screen.findByText("Cited memory")).toBeDefined();
    expect(screen.getByText("Cited design note")).toBeDefined();
  });

  it("approve posts the decision and the queue goes to Inbox zero", async () => {
    server.on("POST /v1/proposals/{proposal_id}/approve", () => {
      queue = [];
      return { proposal: buildProposal({ status: "approved" }), ticket: buildTicket() };
    });
    renderApp("/inbox");
    await screen.findByRole("button", { name: "Approve" });

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByText(/Approved — the ticket is updated\./)).toBeDefined();
    });
    expect(await screen.findByText(/Inbox zero/)).toBeDefined();
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/proposals/prop-1/approve",
    );
    expect(call).toBeDefined();
  });

  it("reject posts the decision", async () => {
    server.on("POST /v1/proposals/{proposal_id}/reject", () => {
      queue = [];
      return buildProposal({ status: "rejected" });
    });
    renderApp("/inbox");
    await screen.findByRole("button", { name: "Reject" });

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(screen.getByText("Rejected.")).toBeDefined());
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/proposals/prop-1/reject",
    );
    expect(call).toBeDefined();
  });

  it("explains a 409 (someone decided first)", async () => {
    server.on("POST /v1/proposals/{proposal_id}/approve", () => {
      queue = [];
      return new Response(JSON.stringify({ detail: "already rejected" }), { status: 409 });
    });
    renderApp("/inbox");
    await screen.findByRole("button", { name: "Approve" });

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByText(/already decided elsewhere/)).toBeDefined());
  });

  it("shows the Inbox-zero state when no proposal is pending", async () => {
    queue = [];
    renderApp("/inbox");
    expect(await screen.findByText(/Inbox zero/)).toBeDefined();
  });

  it("the denied-calls tab lists recent gateway denials from audit", async () => {
    server.on("GET /v1/audit/range", [
      buildAuditCall({ object_ref: "tools/ticket_search", reason: "tool_allowlist" }),
    ]);
    renderApp("/inbox");
    await screen.findByRole("link", { name: "Fix the flux capacitor" });

    fireEvent.click(screen.getByRole("tab", { name: /Denied calls/ }));

    expect(await screen.findByText("ticket_search")).toBeDefined();
    expect(screen.getByText(/denied: tool_allowlist/)).toBeDefined();
  });

  it("the memory-promotions tab shows its v0.2 empty state", async () => {
    renderApp("/inbox");
    await screen.findByRole("link", { name: "Fix the flux capacitor" });

    fireEvent.click(screen.getByRole("tab", { name: /Memory promotions/ }));

    expect(await screen.findByText(/No memory promotions yet/)).toBeDefined();
  });

  it("badges the proposals tab with the pending count", async () => {
    renderApp("/inbox");
    const tab = await screen.findByRole("tab", { name: /Proposals/ });
    expect(within(tab).getByText("1")).toBeDefined();
  });
});

describe("the Inbox sync-conflict tab (E20-T5 / MOD-26 §B4)", () => {
  it("renders a conflict with both candidate values + the field path", async () => {
    server.on("GET /v1/conflicts", [buildConflict()]);
    renderApp("/inbox");
    fireEvent.click(await screen.findByRole("tab", { name: /Sync conflicts/ }));

    expect(await screen.findByText(/tickets\/tick-1/)).toBeDefined();
    expect(screen.getByTestId("conflict-keep-a").textContent).toBe("doing");
    expect(screen.getByTestId("conflict-keep-b").textContent).toBe("todo");
  });

  it("keep-A posts the resolution and clears on success", async () => {
    let open = [buildConflict()];
    server
      .on("GET /v1/conflicts", () => open)
      .on("POST /v1/conflicts/{conflict_id}/resolve", () => {
        open = [];
        return { conflict_id: "cr-1", resolved: true, rebase_required: false };
      });
    renderApp("/inbox");
    fireEvent.click(await screen.findByRole("tab", { name: /Sync conflicts/ }));

    fireEvent.click(await screen.findByRole("button", { name: "Keep A" }));

    await waitFor(() => expect(screen.getByText(/Resolved —/)).toBeDefined());
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/conflicts/cr-1/resolve",
    );
    expect(call).toBeDefined();
  });

  it("surfaces a rebase_required (the field moved) as a re-decide notice", async () => {
    server
      .on("GET /v1/conflicts", [buildConflict()])
      .on("POST /v1/conflicts/{conflict_id}/resolve", {
        conflict_id: "cr-1",
        resolved: false,
        rebase_required: true,
      });
    renderApp("/inbox");
    fireEvent.click(await screen.findByRole("tab", { name: /Sync conflicts/ }));

    fireEvent.click(await screen.findByRole("button", { name: "Keep B" }));

    await waitFor(() =>
      expect(screen.getByText(/re-decide against the current value/)).toBeDefined(),
    );
  });

  it("explains a 409 (no backend) on resolve", async () => {
    server
      .on("GET /v1/conflicts", [buildConflict()])
      .on(
        "POST /v1/conflicts/{conflict_id}/resolve",
        () => new Response(JSON.stringify({ detail: "needs sync" }), { status: 409 }),
      );
    renderApp("/inbox");
    fireEvent.click(await screen.findByRole("tab", { name: /Sync conflicts/ }));

    fireEvent.click(await screen.findByRole("button", { name: "Keep A" }));

    await waitFor(() => expect(screen.getByText(/needs the shared backend/)).toBeDefined());
  });

  it("shows the conflict-zero state when there are none", async () => {
    server.on("GET /v1/conflicts", []);
    renderApp("/inbox");
    fireEvent.click(await screen.findByRole("tab", { name: /Sync conflicts/ }));
    expect(await screen.findByText(/No sync conflicts/)).toBeDefined();
  });
});
