/** E20-T2 (MOD-12) — the Settings tree: all five sections present and linked. */

import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "../lib/session";
import { renderApp } from "../test/render";

beforeEach(() => setToken("test-token"));
afterEach(() => clearToken());

describe("Settings tree", () => {
  it("lists the five top-level sections", () => {
    renderApp("/settings");
    const tree = screen.getByRole("navigation", { name: "Settings sections" });
    for (const label of ["Workspace", "Identity", "Devices", "Sync", "Export"]) {
      expect(within(tree).getByRole("link", { name: label })).toBeDefined();
    }
  });

  it("nests the workspace and identity admin pages under their parent", () => {
    renderApp("/settings");
    const tree = screen.getByRole("navigation", { name: "Settings sections" });
    // Members + Telemetry under Workspace; My Agent under Identity.
    for (const label of ["Members", "Telemetry", "My Agent"]) {
      expect(within(tree).getByRole("link", { name: label })).toBeDefined();
    }
  });

  it("shows the connected session state", () => {
    renderApp("/settings");
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeDefined();
  });
});
