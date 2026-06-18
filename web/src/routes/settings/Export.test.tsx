/**
 * Settings → Export (DEBT-34): the button downloads the shipped /v1/export
 * bundle, and a failed export surfaces an honest fallback instead of a dead
 * control. jsdom has no object-URL/download plumbing, so we stub it and assert
 * the POST + the user-visible outcome.
 */

import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

let server: MockApiServer;
const realCreateObjectURL = URL.createObjectURL;
const realRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  setToken("kq_session.token");
  server = new MockApiServer();
  // jsdom implements neither object URLs nor anchor-driven downloads. Patch the
  // two statics directly so `new URL(...)` (used by the mock dispatch) still works.
  URL.createObjectURL = vi.fn(() => "blob:export");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  server.restore();
  clearToken();
  URL.createObjectURL = realCreateObjectURL;
  URL.revokeObjectURL = realRevokeObjectURL;
  vi.restoreAllMocks();
});

describe("Settings → Export", () => {
  it("downloads the workspace bundle by POSTing /v1/export", async () => {
    const bundle = new Response(new Blob([new Uint8Array([1, 2, 3])]), {
      headers: { "Content-Type": "application/gzip" },
    });
    server.on("POST /v1/export", () => bundle.clone());
    renderApp("/settings/export");

    const button = screen.getByRole("button", { name: "Export workspace" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);

    const status = await screen.findByRole("status");
    expect(status.textContent).toContain("kantaq-export.tar.gz");
    expect(server.calls.some((c) => c.method === "POST" && c.path === "/v1/export")).toBe(true);
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
  });

  it("shows an honest fallback when the export endpoint errors", async () => {
    server.on("POST /v1/export", () => new Response("nope", { status: 409 }));
    renderApp("/settings/export");

    fireEvent.click(screen.getByRole("button", { name: "Export workspace" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Export failed");
    expect(alert.textContent).toContain("data/local.sqlite");
  });
});
