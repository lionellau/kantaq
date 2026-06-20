/**
 * E20-T3 (MOD-12) — one proposal in the Inbox queue: diff + cited memory.
 *
 * Extends the v0.0.5 raw-value row into the v0.1 deliverable: a field-level
 * before→after diff (against the live ticket) and the memory cited for the
 * ticket, with Approve / Reject. Shared by the Inbox proposals tab; the
 * proposed values are untrusted agent text (PRD §15) and render as plain text
 * through `FieldDiff`, never as markup.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import type { LinkedMemory, Proposal, Ticket } from "../api/types";
import { fmtDateTime } from "../lib/format";
import type { MemberDirectory } from "../lib/members";
import * as ui from "../lib/ui";
import ActorName from "./ActorName";
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
  directory,
  busy,
  onDecide,
}: {
  proposal: Proposal;
  ticket: Ticket | null;
  citedMemory: LinkedMemory[];
  directory: MemberDirectory;
  busy: boolean;
  onDecide: (decision: "approve" | "reject", reason?: string) => void;
}) {
  const changes = proposedChanges(proposal);
  const note = proposalNote(proposal);
  // Reject opens an optional "why?" the proposing agent's owner will see; a
  // remote teammate needs the reason, not a silent decline (E20-T6).
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
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
            proposed by <ActorName id={proposal.proposer_id} directory={directory} /> ·{" "}
            {fmtDateTime(proposal.created_at)}
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
            disabled={busy || rejecting}
            onClick={() => setRejecting(true)}
          >
            Reject
          </button>
        </div>
      </div>

      {rejecting && (
        <div style={{ marginTop: 12, display: "grid", gap: 8 }}>
          <label style={ui.label}>
            Reason (optional) — the proposing agent's owner sees this
            <textarea
              aria-label="reject reason"
              style={{ ...ui.input, minHeight: "3rem", resize: "vertical" }}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="why this proposal is declined"
            />
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              style={ui.dangerButton}
              disabled={busy}
              onClick={() => onDecide("reject", reason.trim() || undefined)}
            >
              Confirm reject
            </button>
            <button
              type="button"
              style={ui.button}
              disabled={busy}
              onClick={() => {
                setRejecting(false);
                setReason("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
