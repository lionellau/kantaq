/** E18-T3 — the sync-state badge primitive renders all three states. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SyncBadge, { type SyncState } from "./SyncBadge";

const CASES: Array<{ state: SyncState; label: string }> = [
  { state: "draft", label: "Draft" },
  { state: "proposed", label: "Proposed" },
  { state: "committed", label: "Committed" },
];

describe("SyncBadge", () => {
  it.each(CASES)("renders the $state state", ({ state, label }) => {
    render(<SyncBadge state={state} />);
    const badge = screen.getByRole("status", { name: `sync state: ${label.toLowerCase()}` });
    expect(badge.textContent).toBe(label);
    expect(badge.dataset.state).toBe(state);
  });

  it("distinguishes the three states visually", () => {
    render(
      <>
        <SyncBadge state="draft" />
        <SyncBadge state="proposed" />
        <SyncBadge state="committed" />
      </>,
    );
    const backgrounds = screen.getAllByRole("status").map((badge) => badge.style.background);
    expect(new Set(backgrounds).size).toBe(3);
  });
});
