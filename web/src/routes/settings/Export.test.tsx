/** E20-T2 (MOD-12) — Settings → Export: the stub names the promise, action disabled. */

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp } from "../../test/render";

describe("Settings → Export", () => {
  it("renders the stub with a disabled export action", () => {
    renderApp("/settings/export");
    expect(screen.getByRole("heading", { level: 1, name: "Export" })).toBeDefined();
    expect(screen.getByText(/Not available yet/)).toBeDefined();
    const button = screen.getByRole("button", { name: "Export workspace" });
    expect(button).toBeDefined();
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});
