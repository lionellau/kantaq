import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";

describe("renderApp helper", () => {
  it("renders the app at a given route", () => {
    renderApp("/settings");
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeDefined();
  });
});
