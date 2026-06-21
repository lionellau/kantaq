/**
 * E15-T1 (MOD-29) — the Inbox renders a follow-up proposal's own summary, not a
 * ticket field diff. A follow_up proposal carries a `{kind, ...}` diff; the card
 * detects it and shows what would be created/edited/completed.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { Member, Proposal } from "../api/types";
import type { MemberDirectory } from "../lib/members";
import { buildProposal } from "../test/builders";
import ProposalCard, { followUpKind } from "./ProposalCard";

const directory: MemberDirectory = new Map<string, Member>();

function renderCard(proposal: Proposal) {
  return render(
    <MemoryRouter>
      <ProposalCard
        proposal={proposal}
        ticket={null}
        citedMemory={[]}
        directory={directory}
        busy={false}
        onDecide={() => {}}
      />
    </MemoryRouter>,
  );
}

describe("followUpKind", () => {
  it("returns null for a ticket-change proposal", () => {
    expect(followUpKind(buildProposal())).toBeNull();
  });

  it("detects each follow-up kind", () => {
    expect(
      followUpKind(
        buildProposal({ diff: { kind: "follow_up.create", follow_up: { title: "x" } } }),
      ),
    ).toBe("follow_up.create");
    expect(
      followUpKind(
        buildProposal({ diff: { kind: "follow_up.complete", follow_up_id: "f", status: "done" } }),
      ),
    ).toBe("follow_up.complete");
  });
});

describe("ProposalCard follow-up rendering", () => {
  it("renders a create proposal's title + due, not a field diff", () => {
    renderCard(
      buildProposal({
        diff: {
          kind: "follow_up.create",
          follow_up: { title: "check the deploy", due_at: "2026-09-01T00:00:00Z" },
        },
      }),
    );
    expect(screen.getByText("Proposes a follow-up:")).toBeTruthy();
    expect(screen.getByText("check the deploy")).toBeTruthy();
    expect(screen.getByText(/^due /)).toBeTruthy();
    expect(screen.queryByText("No field changes.")).toBeNull();
  });

  it("renders a complete proposal's target status", () => {
    renderCard(
      buildProposal({
        diff: { kind: "follow_up.complete", follow_up_id: "flw-1", status: "dismissed" },
      }),
    );
    expect(screen.getByText(/Proposes marking a follow-up/)).toBeTruthy();
    expect(screen.getByText("dismissed")).toBeTruthy();
  });

  it("still renders a ticket field diff for a normal proposal", () => {
    renderCard(buildProposal());
    expect(screen.getByText("doing")).toBeTruthy();
  });
});
