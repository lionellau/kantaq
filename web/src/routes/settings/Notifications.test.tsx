/** E20-T9 — Settings → Notifications: default-off, configure, toggle, 403/422. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

const OFF = { enabled: false, sink_type: "webhook", sink_host: null, configured: false };
const ON_SLACK = {
  enabled: true,
  sink_type: "slack",
  sink_host: "hooks.slack.com",
  configured: true,
};

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer().on("GET /v1/notifications", OFF);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → Notifications", () => {
  it("shows the default-off state and the content-free framing", async () => {
    renderApp("/settings/notifications");
    const state = await screen.findByTestId("notifications-state");
    expect(state.textContent).toContain("off");
    expect(screen.getByText(/content-free/)).toBeDefined();
  });

  it("configures a sink and turns on via PUT (host shown, secret never echoed)", async () => {
    server.on("PUT /v1/notifications", ON_SLACK);
    renderApp("/settings/notifications");
    await screen.findByTestId("notifications-state");

    fireEvent.change(screen.getByLabelText("sink type"), { target: { value: "slack" } });
    fireEvent.change(screen.getByLabelText("sink url"), {
      target: { value: "https://hooks.slack.com/services/T/B/SECRET" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save + turn on" }));

    await waitFor(() => {
      expect(screen.getByTestId("notifications-state").textContent).toContain("on");
    });
    expect(screen.getByTestId("notifications-state").textContent).toContain("hooks.slack.com");

    const call = server.calls.find((c) => c.method === "PUT" && c.path === "/v1/notifications");
    expect(call).toBeDefined();
    const body = (await call?.request.json()) as Record<string, unknown>;
    expect(body).toEqual({
      enabled: true,
      sink_type: "slack",
      webhook_url: "https://hooks.slack.com/services/T/B/SECRET",
    });
    // The URL field is cleared — the page never retains the secret.
    expect((screen.getByLabelText("sink url") as HTMLInputElement).value).toBe("");
  });

  it("toggles a configured sink off without re-sending the stored URL", async () => {
    server.on("GET /v1/notifications", ON_SLACK);
    server.on("PUT /v1/notifications", { ...ON_SLACK, enabled: false });
    renderApp("/settings/notifications");

    await waitFor(() => {
      expect(screen.getByTestId("notifications-state").textContent).toContain("on");
    });
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));

    await waitFor(() => {
      expect(screen.getByTestId("notifications-state").textContent).toContain("off");
    });
    const call = server.calls.find((c) => c.method === "PUT" && c.path === "/v1/notifications");
    const body = (await call?.request.json()) as { webhook_url: unknown };
    expect(body.webhook_url).toBeNull(); // never re-sends the stored secret URL
  });

  it("surfaces the 403 for a non-admin", async () => {
    server.on("PUT /v1/notifications", () => new Response(null, { status: 403 }));
    renderApp("/settings/notifications");
    await screen.findByTestId("notifications-state");

    fireEvent.change(screen.getByLabelText("sink url"), {
      target: { value: "https://hooks.example.com/x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save + turn on" }));

    expect(await screen.findByText(/only an Owner or Maintainer/)).toBeDefined();
  });

  it("surfaces the 422 when enabling without a valid URL", async () => {
    server.on("PUT /v1/notifications", () => new Response(null, { status: 422 }));
    renderApp("/settings/notifications");
    await screen.findByTestId("notifications-state");

    fireEvent.click(screen.getByRole("button", { name: "Save + turn on" }));

    expect(await screen.findByText(/valid http\(s\) sink URL/)).toBeDefined();
  });
});
