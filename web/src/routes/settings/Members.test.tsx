/** E21-T1 — Members: list, invite, revoke, rotate; the token shows exactly once. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildMember } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer().on("GET /v1/members", [
    buildMember(),
    buildMember({ id: "member-2", email: "dev@example.com", role: "Member" }),
  ]);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → Members", () => {
  it("lists the workspace members", async () => {
    renderApp("/settings/members");
    expect(await screen.findByText("owner@example.com")).toBeDefined();
    expect(screen.getByText("dev@example.com")).toBeDefined();
    expect(screen.getByText("Owner")).toBeDefined();
  });

  it("invites a member and shows the minted token exactly once", async () => {
    server.on("POST /v1/members/invite", {
      member: buildMember({ id: "member-3", email: "new@example.com", role: "Member" }),
      token: "kq_new.secret-token",
    });
    renderApp("/settings/members");
    await screen.findByText("owner@example.com");

    fireEvent.change(screen.getByLabelText("Invite by email"), {
      target: { value: "new@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));

    const token = await screen.findByTestId("minted-token");
    expect(token.textContent).toBe("kq_new.secret-token");
    expect(screen.getByText(/shown once/)).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("minted-token")).toBeNull();
    expect(document.body.textContent).not.toContain("kq_new.secret-token");
  });

  it("an Agent invite carries the propose-first scopes", async () => {
    server.on("POST /v1/members/invite", {
      member: buildMember({ id: "member-4", email: "bot@example.com", role: "Agent" }),
      token: "kq_agent.secret",
    });
    renderApp("/settings/members");
    await screen.findByText("owner@example.com");

    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "Agent" } });
    expect(screen.getByText(/tickets\.read, proposals\.write/)).toBeDefined();
    fireEvent.change(screen.getByLabelText("Invite by email"), {
      target: { value: "bot@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));

    await waitFor(() => {
      const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/members/invite");
      expect(call).toBeDefined();
    });
    const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/members/invite");
    const body = (await call?.request.json()) as { role: string; scopes: string[] };
    expect(body.role).toBe("Agent");
    expect(body.scopes).toEqual(["tickets.read", "proposals.write"]);
  });

  it("rotate shows the fresh token once", async () => {
    server.on("POST /v1/members/{member_id}/rotate", {
      member_id: "member-2",
      token: "kq_rotated.secret",
    });
    renderApp("/settings/members");
    await screen.findByText("dev@example.com");

    fireEvent.click(screen.getAllByRole("button", { name: "Rotate token" })[1] as Element);

    const token = await screen.findByTestId("minted-token");
    expect(token.textContent).toBe("kq_rotated.secret");
  });

  it("keeps the invite permission guard when the member list finishes loading (no race)", async () => {
    // Regression for the UAT finding: refresh() used to share one error state
    // with the invite action, so a member-list fetch that resolved AFTER an
    // invite was denied wiped the guard. Defer the list load past the 403 and
    // assert the guard survives (DEBT-32).
    let resolveMembers: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      resolveMembers = resolve;
    });
    server.on("GET /v1/members", async () => {
      await gate;
      return [buildMember()];
    });
    server.on("POST /v1/members/invite", () =>
      Response.json({ detail: "forbidden" }, { status: 403 }),
    );

    renderApp("/settings/members");
    // The invite form renders before the (still-pending) member list.
    fireEvent.change(await screen.findByLabelText("Invite by email"), {
      target: { value: "denied@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    expect(await screen.findByText("your role may not invite members")).toBeDefined();

    // The list load now resolves — it must NOT clear the action error.
    resolveMembers?.();
    expect(await screen.findByText("owner@example.com")).toBeDefined();
    expect(screen.getByText("your role may not invite members")).toBeDefined();
  });

  it("revoke calls the API", async () => {
    server.on("POST /v1/members/{member_id}/revoke", () =>
      buildMember({ id: "member-2", email: "dev@example.com", status: "revoked" }),
    );
    renderApp("/settings/members");
    await screen.findByText("dev@example.com");

    fireEvent.click(screen.getAllByRole("button", { name: "Revoke" })[1] as Element);

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "POST" && c.path === "/v1/members/member-2/revoke",
      );
      expect(call).toBeDefined();
    });
  });
});
