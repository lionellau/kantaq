/**
 * E20-T3 (MOD-12) — one proposal in the Inbox queue: diff + cited memory.
 *
 * Extends the v0.0.5 raw-value row into the v0.1 deliverable: a field-level
 * before→after diff (against the live ticket) and the memory cited for the
 * ticket, with Approve / Reject. Shared by the Inbox proposals tab; the
 * proposed values are untrusted agent text (PRD §15) and render as plain text
 * through `FieldDiff`, never as markup.
 */

import { Link } from "react-router-dom";
import type { LinkedMemory, Proposal, Ticket } from "../api/types";
import { fmtDateTime } from "../lib/format";
import * as ui from "../lib/ui";
import CitedMemory from "./CitedMemory";
import FieldDiff from "./FieldDiff";

/** The `(field, proposedValue)` pairs an agent wants to change. */
export function proposedChanges(proposal: Proposal): [string, unknown][] {
  const changes = (proposal.diff as { changes?: Record<string, unknown> }).changes ?? {};
  return Object.entries(changes);
}

export function proposalNote(proposal: Proposal): string {
  const note = (proposal.diff as { note?: unknown }).note;
  return typeof note === "string" ? note : "";
}

export default function ProposalCard({
  proposal,
  ticket,
  citedMemory,
  busy,
  onDecide,
}: {
  proposal: Proposal;
  ticket: Ticket | null;
  citedMemory: LinkedMemory[];
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}) {
  const changes = proposedChanges(proposal);
  const note = proposalNote(proposal);
  // `before` is the live ticket value; null when the ticket has not loaded yet
  // (the diff still shows the proposed side, never a guessed before).
  const current = (ticket ?? {}) as Record<string, unknown>;

  return (
    <li style={ui.card} aria-label={`proposal ${proposal.id}`}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontWeight: 600 }}>
            <Link to={`/tickets/${proposal.ticket_id}`}>
              {proposal.ticket_title ?? proposal.ticket_id}
            </Link>
          </div>
          <div style={ui.muted}>
            proposed by {proposal.proposer_id} · {fmtDateTime(proposal.created_at)}
          </div>

          <div style={{ display: "grid", gap: 8, margin: "0.6rem 0 0" }}>
            {changes.length === 0 ? (
              <p style={{ ...ui.muted, margin: 0 }}>No field changes.</p>
            ) : (
              changes.map(([field, value]) => (
                <FieldDiff
                  key={field}
                  field={field}
                  before={ticket === null ? undefined : current[field]}
                  after={value}
                />
              ))
            )}
          </div>

          {note !== "" && <p style={{ ...ui.muted, margin: "0.6rem 0 0" }}>note: {note}</p>}

          <CitedMemory items={citedMemory} ticketId={proposal.ticket_id} />
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexShrink: 0 }}>
          <button
            type="button"
            style={ui.primaryButton}
            disabled={busy}
            onClick={() => onDecide("approve")}
          >
            Approve
          </button>
          <button
            type="button"
            style={ui.dangerButton}
            disabled={busy}
            onClick={() => onDecide("reject")}
          >
            Reject
          </button>
        </div>
      </div>
    </li>
  );
}
