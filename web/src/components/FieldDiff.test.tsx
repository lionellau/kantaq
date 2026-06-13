/** E20-T3 — the field-level proposal diff: value formatting + strike-through. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import FieldDiff, { displayValue } from "./FieldDiff";

describe("displayValue", () => {
  it("renders empties as an em dash", () => {
    expect(displayValue(null)).toBe("—");
    expect(displayValue(undefined)).toBe("—");
    expect(displayValue("")).toBe("—");
    expect(displayValue([])).toBe("—");
  });

  it("joins arrays and passes strings through (no JSON quotes)", () => {
    expect(displayValue(["ui", "sprint-2"])).toBe("ui, sprint-2");
    expect(displayValue("doing")).toBe("doing");
    expect(displayValue(3)).toBe("3");
  });
});

describe("FieldDiff", () => {
  it("strikes the before value when it changes", () => {
    render(<FieldDiff field="status" before="todo" after="doing" />);
    const before = screen.getByText("todo");
    expect(before.style.textDecoration).toBe("line-through");
    expect(screen.getByText("doing")).toBeDefined();
  });

  it("does not strike an unchanged value", () => {
    render(<FieldDiff field="priority" before="high" after="high" />);
    // Both sides read "high"; the before side carries no strike-through.
    const cells = screen.getAllByText("high");
    expect(cells[0].style.textDecoration).toBe("none");
  });
});
