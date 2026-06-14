/**
 * E21-T3 (MOD-13, MOD-19) — the onboarding wizard: connect, create the first
 * project, and seed its project-brief memory, then hand off to My Agent.
 */

import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { buildMemoryEntry, buildProject } from "../test/builders";
import { MockApiServer } from "../test/mockApi";
import { renderApp } from "../test/render";
import { briefBody } from "./Onboarding";

let server: MockApiServer;

beforeEach(() => {
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the onboarding wizard", () => {
  it("asks to connect first when there is no session", () => {
    clearToken();
    renderApp("/onboarding");
    expect(screen.getByRole("heading", { level: 1, name: "Welcome to kantaq" })).toBeDefined();
    expect(screen.getByText("Connect to your runtime")).toBeDefined();
    // No first-project form until connected.
    expect(screen.queryByText("Create your first project")).toBeNull();
  });

  it("advances to the project step once a token is entered", () => {
    clearToken();
    renderApp("/onboarding");
    fireEvent.change(screen.getByLabelText("Runtime token"), { target: { value: "tok-123" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(screen.getByText("Create your first project")).toBeDefined();
  });

  it("creates the project and seeds a project-brief memory, then shows the agent step", async () => {
    setToken("tok-123");
    server
      .on("POST /v1/projects", () =>
        Response.json(buildProject({ id: "proj-9", name: "Apollo" }), { status: 201 }),
      )
      .on("POST /v1/memory", () => Response.json(buildMemoryEntry(), { status: 201 }));
    renderApp("/onboarding");

    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Apollo" } });
    fireEvent.change(screen.getByLabelText("Goal"), { target: { value: "Ship v1" } });
    fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "MVP only" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project & seed brief" }));

    // The agent step appears after both writes land.
    expect(await screen.findByText("Connect your agent")).toBeDefined();
    expect(screen.getByRole("link", { name: /Open the connection snippet/ })).toBeDefined();

    const projectCall = server.calls.find((c) => c.method === "POST" && c.path === "/v1/projects");
    const projectBody = (await projectCall?.request.json()) as { name: string; goal: string };
    expect(projectBody.name).toBe("Apollo");
    expect(projectBody.goal).toBe("Ship v1");

    const memoryCall = server.calls.find((c) => c.method === "POST" && c.path === "/v1/memory");
    expect(memoryCall).toBeDefined();
    const memoryBody = (await memoryCall?.request.json()) as {
      title: string;
      body: string;
      type: string;
      space: string;
      visibility: string;
      linked_entities: string[];
    };
    expect(memoryBody.title).toBe("Apollo — project brief");
    expect(memoryBody.type).toBe("note");
    expect(memoryBody.space).toBe("project");
    expect(memoryBody.visibility).toBe("team");
    expect(memoryBody.linked_entities).toEqual(["projects/proj-9"]);
    expect(memoryBody.body).toContain("Goal: Ship v1");
    expect(memoryBody.body).toContain("Scope: MVP only");
  });

  it("still advances (with a warning) if the brief seed fails", async () => {
    setToken("tok-123");
    server
      .on("POST /v1/projects", () =>
        Response.json(buildProject({ id: "proj-9", name: "Apollo" }), { status: 201 }),
      )
      .on("POST /v1/memory", () => Response.json({ detail: "nope" }, { status: 500 }));
    renderApp("/onboarding");

    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Apollo" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project & seed brief" }));

    expect(await screen.findByText("Connect your agent")).toBeDefined();
    expect(screen.getByText(/brief could not be seeded/)).toBeDefined();
  });

  it("refuses to submit a project without a name", async () => {
    setToken("tok-123");
    renderApp("/onboarding");
    fireEvent.click(screen.getByRole("button", { name: "Create project & seed brief" }));
    expect(await screen.findByText("give your project a name")).toBeDefined();
    expect(server.calls.some((c) => c.method === "POST")).toBe(false);
  });
});

describe("briefBody", () => {
  it("composes goal and scope", () => {
    expect(briefBody("Apollo", "Ship v1", "MVP only")).toBe("Goal: Ship v1\n\nScope: MVP only");
  });

  it("falls back to a default when goal and scope are blank", () => {
    expect(briefBody("Apollo", "", "  ")).toBe("Project brief for Apollo.");
  });
});
