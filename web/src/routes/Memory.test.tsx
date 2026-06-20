/** E13-T3 — Memory page: list with privacy badges, filters, create, link. */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { buildMemoryEntry, buildMemoryLink, buildTicket } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer().on("GET /v1/tickets", [buildTicket()]);
});

afterEach(() => {
  server.restore();
  clearToken();
});

function memoryCalls(): string[] {
  return server.calls
    .filter((call) => call.method === "GET" && call.path === "/v1/memory")
    .map((call) => new URL(call.request.url).search);
}

describe("the memory page", () => {
  it("renders entries with visibility badges and provenance-bearing fields", async () => {
    server.on("GET /v1/memory", [
      buildMemoryEntry({ id: "mem-1", title: "Team decision" }),
      buildMemoryEntry({
        id: "mem-2",
        title: "My private note",
        visibility: "local",
        domain_visibility: "private_local",
      }),
    ]);
    renderApp("/memory");

    expect(await screen.findByText("Team decision")).toBeDefined();
    expect(screen.getByText("My private note")).toBeDefined();
    expect(screen.getByLabelText("visibility: private to this machine")).toBeDefined();
    expect(screen.getByLabelText("visibility: personal_synced")).toBeDefined();
  });

  it("sends each filter as its query parameter", async () => {
    server.on("GET /v1/memory", []);
    renderApp("/memory");
    await screen.findByText("No memory entries.");

    const filters = within(screen.getByRole("form", { name: "Memory filters" }));
    fireEvent.change(filters.getByLabelText("Type"), { target: { value: "decision" } });
    await waitFor(() => {
      expect(memoryCalls().at(-1)).toBe("?type=decision");
    });

    fireEvent.change(filters.getByLabelText("Search"), { target: { value: "jwt" } });
    await waitFor(() => {
      expect(memoryCalls().at(-1)).toContain("q=jwt");
    });
  });

  it("creates an entry from the form, including a private one", async () => {
    server.on("GET /v1/memory", []);
    server.on(
      "POST /v1/memory",
      () => new Response(JSON.stringify(buildMemoryEntry()), { status: 201 }),
    );
    renderApp("/memory");
    await screen.findByText("No memory entries.");

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Keychain quirk" },
    });
    fireEvent.change(screen.getByLabelText("Visibility"), { target: { value: "local" } });
    fireEvent.submit(screen.getByRole("form", { name: "Create memory entry" }));

    await waitFor(() => {
      const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/memory");
      expect(call).toBeDefined();
    });
    const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/memory");
    const body = (await call?.request.json()) as { title: string; visibility: string };
    expect(body.title).toBe("Keychain quirk");
    expect(body.visibility).toBe("local");
  });

  it("links an entry to a ticket with a reason", async () => {
    server.on("GET /v1/memory", [buildMemoryEntry()]);
    server.on(
      "POST /v1/memory/{memory_id}/link",
      () => new Response(JSON.stringify(buildMemoryLink()), { status: 201 }),
    );
    renderApp("/memory");

    fireEvent.click(await screen.findByRole("button", { name: "Link to ticket" }));
    const linkForm = await screen.findByRole("form", { name: "Link memory to ticket" });
    expect(linkForm).toBeDefined();

    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "explains the flux capacitor" },
    });
    fireEvent.submit(linkForm);

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "POST" && c.path === "/v1/memory/mem-1/link",
      );
      expect(call).toBeDefined();
    });
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/memory/mem-1/link",
    );
    const body = (await call?.request.json()) as { ticket_id: string; reason: string };
    expect(body.ticket_id).toBe("tick-1");
    expect(body.reason).toBe("explains the flux capacitor");
  });

  it("asks to connect when there is no session", () => {
    clearToken();
    renderApp("/memory");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });
});

describe("promote to team (E13-T6 / MOD-19 — closes DEBT-28)", () => {
  it("promotes a local entry into the Inbox approval queue", async () => {
    server.on("GET /v1/memory", [
      buildMemoryEntry({
        id: "mem-5",
        title: "Private learning",
        visibility: "local",
        domain_visibility: "private_local",
      }),
    ]);
    server.on(
      "POST /v1/memory/{memory_id}/promote",
      () =>
        new Response(JSON.stringify(buildMemoryEntry({ id: "mem-6", review_status: "proposed" })), {
          status: 201,
        }),
    );
    renderApp("/memory");

    fireEvent.click(await screen.findByRole("button", { name: "Promote to team" }));

    await waitFor(() => expect(screen.getByText(/awaiting approval in the Inbox/)).toBeDefined());
    const call = server.calls.find(
      (c) => c.method === "POST" && c.path === "/v1/memory/mem-5/promote",
    );
    expect(call).toBeDefined();
  });

  it("offers promote on a team draft but not on an already-proposed entry", async () => {
    server.on("GET /v1/memory", [
      buildMemoryEntry({ id: "mem-draft", title: "Draft note", review_status: "draft" }),
      buildMemoryEntry({ id: "mem-prop", title: "Already proposed", review_status: "proposed" }),
    ]);
    renderApp("/memory");

    await screen.findByText("Draft note");
    // Exactly one Promote button: the draft has it, the proposed entry does not.
    expect(screen.getAllByRole("button", { name: "Promote to team" })).toHaveLength(1);
  });

  it("explains a 422 (the entry can't be promoted from its current state)", async () => {
    server.on("GET /v1/memory", [buildMemoryEntry({ id: "mem-5", review_status: "draft" })]);
    server.on(
      "POST /v1/memory/{memory_id}/promote",
      () => new Response(JSON.stringify({ detail: "bad state" }), { status: 422 }),
    );
    renderApp("/memory");

    fireEvent.click(await screen.findByRole("button", { name: "Promote to team" }));
    await waitFor(() =>
      expect(screen.getByText(/can't be promoted from its current state/)).toBeDefined(),
    );
  });
});
