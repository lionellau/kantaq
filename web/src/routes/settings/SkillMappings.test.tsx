/** E17-T5 (MOD-22) — Settings → Skill mappings: list, create, toggle, delete. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../../lib/session";
import { buildSkillContainer, buildSkillMapping } from "../../test/builders";
import { MockApiServer } from "../../test/mockApi";
import { renderApp } from "../../test/render";

let server: MockApiServer;

const CONTAINER = buildSkillContainer({ id: "skc-1", name: "Code review" });
const MAPPING = buildSkillMapping({
  id: "skm-1",
  container_id: "skc-1",
  connection: "My Claude Code",
  status: "active",
});

beforeEach(() => {
  setToken("test-token");
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("Settings → Skill mappings", () => {
  it("lists mappings with the container name and the mapped tool", async () => {
    server.on("GET /v1/skill-containers", [CONTAINER]).on("GET /v1/skill-mappings", [MAPPING]);
    renderApp("/settings/skill-mappings");

    // "Code review" appears in both the picker option and the row, so anchor on
    // the unique mapped-tool label, then assert the container name renders too.
    expect(await screen.findByText("My Claude Code")).toBeDefined();
    expect(screen.getAllByText("Code review").length).toBeGreaterThan(0);
  });

  it("creates a mapping through the registry endpoint", async () => {
    server
      .on("GET /v1/skill-containers", [CONTAINER])
      .on("GET /v1/skill-mappings", [])
      .on("POST /v1/skill-mappings", buildSkillMapping());
    renderApp("/settings/skill-mappings");

    await screen.findByText(/No mappings yet/);
    fireEvent.change(screen.getByLabelText("Skill container"), { target: { value: "skc-1" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. My Claude Code"), {
      target: { value: "Codex" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add mapping" }));

    await waitFor(() => {
      const call = server.calls.find((c) => c.method === "POST" && c.path === "/v1/skill-mappings");
      expect(call).toBeDefined();
    });
  });

  it("toggles a mapping's status", async () => {
    server
      .on("GET /v1/skill-containers", [CONTAINER])
      .on("GET /v1/skill-mappings", [MAPPING])
      .on("PATCH /v1/skill-mappings/{mapping_id}", buildSkillMapping({ status: "disabled" }));
    renderApp("/settings/skill-mappings");
    await screen.findByText("My Claude Code");

    fireEvent.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "PATCH" && c.path === "/v1/skill-mappings/skm-1",
      );
      expect(call).toBeDefined();
    });
  });

  it("deletes a mapping", async () => {
    server
      .on("GET /v1/skill-containers", [CONTAINER])
      .on("GET /v1/skill-mappings", [MAPPING])
      .on("DELETE /v1/skill-mappings/{mapping_id}", () => new Response(null, { status: 204 }));
    renderApp("/settings/skill-mappings");
    await screen.findByText("My Claude Code");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      const call = server.calls.find(
        (c) => c.method === "DELETE" && c.path === "/v1/skill-mappings/skm-1",
      );
      expect(call).toBeDefined();
    });
  });

  it("guards when not connected", () => {
    clearToken();
    renderApp("/settings/skill-mappings");
    expect(screen.getByText(/Not connected/)).toBeDefined();
  });
});
