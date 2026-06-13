/** E20-T2 (MOD-12) — Settings → Devices: trust roots + decommission control. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildDevice } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

let server: MockApiServer;

const CURRENT = buildDevice({ id: "dev-1", label: "local runtime", is_current: true });
const OTHER = buildDevice({
  id: "dev-2",
  label: "laptop",
  is_current: false,
  member_email: "bob@example.com",
  public_key: "c".repeat(64),
});

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → Devices", () => {
  it("lists devices, marks the current one, and masks the key", async () => {
    server.on("GET /v1/devices", [CURRENT, OTHER]);
    renderApp("/settings/devices");

    expect(await screen.findByText("local runtime")).toBeDefined();
    expect(screen.getByText("laptop")).toBeDefined();
    expect(screen.getByText("this runtime")).toBeDefined();
    expect(screen.getByText("bob@example.com")).toBeDefined();
    // The full 64-char key is never printed in full; it is masked.
    expect(screen.queryByText("c".repeat(64))).toBeNull();
  });

  it("protects this runtime's own device from decommission", async () => {
    server.on("GET /v1/devices", [CURRENT, OTHER]);
    renderApp("/settings/devices");
    await screen.findByText("local runtime");

    const buttons = screen.getAllByRole("button", { name: "Decommission" });
    // Row order matches the response: current first (disabled), other second.
    expect((buttons[0] as HTMLButtonElement).disabled).toBe(true);
    expect((buttons[1] as HTMLButtonElement).disabled).toBe(false);
  });

  it("decommissions another device through the MOD-06 path", async () => {
    server
      .on("GET /v1/devices", [CURRENT, OTHER])
      .on("POST /v1/devices/{device_id}/revoke", buildDevice({ id: "dev-2", active: false }));
    renderApp("/settings/devices");
    await screen.findByText("laptop");

    fireEvent.click(screen.getAllByRole("button", { name: "Decommission" })[1]);

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "POST" && c.path === "/v1/devices/dev-2/revoke",
      );
      expect(call).toBeDefined();
    });
  });

  it("surfaces the 403 when a non-admin tries to decommission", async () => {
    server
      .on("GET /v1/devices", [CURRENT, OTHER])
      .on("POST /v1/devices/{device_id}/revoke", () => new Response(null, { status: 403 }));
    renderApp("/settings/devices");
    await screen.findByText("laptop");

    fireEvent.click(screen.getAllByRole("button", { name: "Decommission" })[1]);

    expect(
      await screen.findByText(/only a Maintainer or Owner may decommission a device/),
    ).toBeDefined();
  });

  it("guards when not connected", () => {
    clearToken();
    renderApp("/settings/devices");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });
});
