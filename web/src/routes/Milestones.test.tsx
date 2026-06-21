/** E14-T3 — Milestones page: list with status + ticket count, create, actions. */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { buildMilestone, buildProject } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";

let server: MockApiServer;

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer().on("GET /v1/projects", [buildProject()]);
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the milestones page", () => {
  it("renders milestones with status and ticket count", async () => {
    server.on("GET /v1/milestones", [
      buildMilestone({ id: "m-1", name: "v1.0 launch", status: "active", ticket_count: 3 }),
      buildMilestone({ id: "m-2", name: "v2.0", status: "complete", ticket_count: 0 }),
    ]);
    renderApp("/milestones");

    expect(await screen.findByText("v1.0 launch")).toBeDefined();
    expect(screen.getByText("v2.0")).toBeDefined();
    // The completed milestone shows its status chip (scoped to the table so the
    // create form's status <option> doesn't match too).
    const table = screen.getByRole("table");
    expect(within(table).getByText("complete")).toBeDefined();
  });

  it("creates a milestone from the form", async () => {
    server.on("GET /v1/milestones", []);
    server.on(
      "POST /v1/milestones",
      () => new Response(JSON.stringify(buildMilestone()), { status: 201 }),
    );
    renderApp("/milestones");
    await screen.findByText("No milestones yet.");

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Beta cutover" } });
    fireEvent.submit(screen.getByRole("form", { name: "Create milestone" }));

    await waitFor(() => {
      expect(
        server.calls.find((c) => c.method === "POST" && c.path === "/v1/milestones"),
      ).toBeDefined();
    });
    const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/milestones");
    const body = (await call?.request.json()) as { name: string; project_id: string };
    expect(body.name).toBe("Beta cutover");
    expect(body.project_id).toBe("proj-1");
  });

  it("completes a milestone via the row action", async () => {
    server.on("GET /v1/milestones", [buildMilestone({ id: "m-1", status: "active" })]);
    server.on(
      "PATCH /v1/milestones/{milestone_id}",
      () => new Response(JSON.stringify(buildMilestone({ status: "complete" })), { status: 200 }),
    );
    renderApp("/milestones");

    fireEvent.click(await screen.findByRole("button", { name: "Complete" }));
    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "PATCH" && c.path === "/v1/milestones/m-1",
      );
      expect(call).toBeDefined();
    });
  });
});
