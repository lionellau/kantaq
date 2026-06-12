/** E28-T1 — Telemetry: default-off toggle, metrics, and the raw event list. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildTelemetryView } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer().on("GET /v1/telemetry", buildTelemetryView());
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → Telemetry", () => {
  it("shows the default-off state and the privacy framing", async () => {
    renderApp("/settings/telemetry");
    const state = await screen.findByTestId("telemetry-state");
    expect(state.textContent).toContain("off");
    expect(screen.getByText(/never recorded/)).toBeDefined();
    expect(screen.getByText("Nothing recorded.")).toBeDefined();
  });

  it("turns telemetry on via PUT and reflects the new state", async () => {
    server.on(
      "PUT /v1/telemetry",
      buildTelemetryView({ enabled: true, metrics: { events_total: 0 } }),
    );
    renderApp("/settings/telemetry");
    await screen.findByTestId("telemetry-state");

    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));

    await waitFor(() => {
      expect(screen.getByTestId("telemetry-state").textContent).toContain("on");
    });
    const call = server.calls.find((c) => c.method === "PUT" && c.path === "/v1/telemetry");
    expect(call).toBeDefined();
    const body = (await call?.request.json()) as { enabled: boolean };
    expect(body.enabled).toBe(true);
  });

  it("renders the computed metrics and the raw events", async () => {
    server.on(
      "GET /v1/telemetry",
      buildTelemetryView({
        enabled: true,
        metrics: {
          events_total: 2,
          proposal_acceptance_rate: 0.5,
          median_seconds_to_approve: 120,
          mcp_sessions_total: 3,
          repeat_session_members: 1,
          activity_views_total: 4,
          install_to_first_proposal_seconds: 7200,
          weekly_active: true,
        },
        events: [
          {
            id: "evt-2",
            name: "proposal_approved",
            props: { seconds_to_decision: 120 },
            created_at: "2026-01-02T00:00:00",
          },
          {
            id: "evt-1",
            name: "proposals_listed",
            props: { count: 3 },
            created_at: "2026-01-01T00:00:00",
          },
        ],
      }),
    );
    renderApp("/settings/telemetry");

    expect(await screen.findByText("50%")).toBeDefined();
    expect(screen.getByText("2m")).toBeDefined();
    expect(screen.getByText("2.0h")).toBeDefined();
    expect(screen.getByText("proposal_approved")).toBeDefined();
    expect(screen.getByText(/"count":3/)).toBeDefined();
  });

  it("surfaces the 403 when a non-admin flips the toggle", async () => {
    server.on("PUT /v1/telemetry", () => new Response(null, { status: 403 }));
    renderApp("/settings/telemetry");
    await screen.findByTestId("telemetry-state");

    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));

    expect(
      await screen.findByText(/only an Owner or Maintainer may change the telemetry setting/),
    ).toBeDefined();
  });
});
