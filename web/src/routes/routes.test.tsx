import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { routes } from "../router";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
}

describe("app shell routes", () => {
  it.each([
    ["/", "Backlog"],
    ["/memory", "Memory"],
    ["/inbox", "Inbox"],
    ["/agents", "Agents"],
    ["/settings", "Settings"],
  ])("renders %s with an <h1> '%s'", (path, heading) => {
    renderAt(path);
    expect(screen.getByRole("heading", { level: 1, name: heading })).toBeDefined();
  });

  it("renders the 5 primary nav links on every page", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(nav).toBeDefined();
    for (const label of ["Backlog", "Memory", "Inbox", "Agents", "Settings"]) {
      expect(screen.getByRole("link", { name: label })).toBeDefined();
    }
  });
});
