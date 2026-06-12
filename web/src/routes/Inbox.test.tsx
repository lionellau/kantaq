/** E20-T1 — the Inbox: pending queue, approve and reject flows, 409 race. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Proposal } from "../api/types";
import { clearToken, setToken } from "../lib/session";
import { buildProposal, buildTicket } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;
let queue: Proposal[];

beforeEach(() => {
  setToken("test-token");
  queue = [buildProposal()];
  server = new MockApiServer().on("GET /v1/proposals", () => queue);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the Inbox queue", () => {
  it("lists pending proposals with ticket, proposer, and the diff", async () => {
    renderApp("/inbox");

    expect(await screen.findByRole("link", { name: "Fix the flux capacitor" })).toBeDefined();
    expect(screen.getByText(/proposed by agent-1/)).toBeDefined();
    expect(screen.getByText("status")).toBeDefined();
    expect(screen.getByText('"doing"')).toBeDefined();
    expect(screen.getByText(/note: ready to start/)).toBeDefined();
  });

  it("approve posts the decision and the row leaves the queue", async () => {
    server.on("POST /v1/proposals/{proposal_id}/approve", () => {
      queue = [];
      return {
        proposal: buildProposal({ status: "approved" }),
        ticket: buildTicket({ status: "doing" }),
      };
    });
    renderApp("/inbox");
    await screen.findByRole("button", { name: "Approve" });

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByText(/Approved — the ticket is updated\./)).toBeDefined();
    });
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    });
    expect(screen.getByText("No pending proposals.")).toBeDefined();
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

    await waitFor(() => {
      expect(screen.getByText("Rejected.")).toBeDefined();
    });
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/proposals/prop-1/reject",
    );
    expect(call).toBeDefined();
  });

  it("explains a 409 (someone decided first) and refreshes the queue", async () => {
    server.on("POST /v1/proposals/{proposal_id}/approve", () => {
      queue = [];
      return new Response(JSON.stringify({ detail: "already rejected" }), { status: 409 });
    });
    renderApp("/inbox");
    await screen.findByRole("button", { name: "Approve" });

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByText(/already decided elsewhere/)).toBeDefined();
    });
    expect(screen.getByText("No pending proposals.")).toBeDefined();
  });

  it("shows the empty state", async () => {
    queue = [];
    renderApp("/inbox");
    expect(await screen.findByText("No pending proposals.")).toBeDefined();
  });
});
