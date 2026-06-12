/**
 * E20-T1 (MOD-12) — the Inbox: one queue of pending agent proposals.
 *
 * The queue is the local replica's `agent_proposals` rows, so proposals from
 * every member's agent arrive here through sync (FR-E20-1) on the 2 s poll.
 * Approve applies the proposed change through the runtime's one write path
 * and flips the proposal; Reject declines it; both decisions sync back. A
 * 409 means someone else decided first — the row refreshes away.
 *
 * Proposed values are untrusted agent/human text: rendered as plain text,
 * never as markup.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Proposal } from "../api/types";
import { fmtDateTime } from "../lib/format";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

function proposedChanges(proposal: Proposal): [string, string][] {
  const changes = (proposal.diff as { changes?: Record<string, unknown> }).changes ?? {};
  return Object.entries(changes).map(([field, value]) => [field, JSON.stringify(value) ?? ""]);
}

function proposalNote(proposal: Proposal): string {
  const note = (proposal.diff as { note?: unknown }).note;
  return typeof note === "string" ? note : "";
}

export default function Inbox() {
  const { connected } = useSession();
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/proposals", {
      params: { query: { status: "pending" } },
    });
    if (apiError !== undefined) {
      setError("could not load the queue");
      return;
    }
    setError(null);
    setProposals(data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(() => void refresh(), 2000, connected);

  async function decide(proposal: Proposal, decision: "approve" | "reject") {
    setBusy(proposal.id);
    setNotice(null);
    const path = { params: { path: { proposal_id: proposal.id } } };
    const { response, error: apiError } =
      decision === "approve"
        ? await api.POST("/v1/proposals/{proposal_id}/approve", path)
        : await api.POST("/v1/proposals/{proposal_id}/reject", path);
    setBusy(null);
    if (apiError !== undefined) {
      setNotice(
        response?.status === 409
          ? "that proposal was already decided elsewhere"
          : `could not ${decision} the proposal`,
      );
    } else {
      setNotice(decision === "approve" ? "Approved — the ticket is updated." : "Rejected.");
    }
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Inbox</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1>Inbox</h1>
      <p style={ui.muted}>Agent proposals wait here until a human decides.</p>
      {error !== null && <p style={ui.errorText}>{error}</p>}
      {notice !== null && (
        <p>
          <output>{notice}</output>
        </p>
      )}
      {proposals !== null && proposals.length === 0 && (
        <p style={ui.muted}>No pending proposals.</p>
      )}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 12 }}>
        {proposals?.map((proposal) => (
          <li key={proposal.id} style={ui.card} aria-label={`proposal ${proposal.id}`}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}>
                  <Link to={`/tickets/${proposal.ticket_id}`}>
                    {proposal.ticket_title ?? proposal.ticket_id}
                  </Link>
                </div>
                <div style={ui.muted}>
                  proposed by {proposal.proposer_id} · {fmtDateTime(proposal.created_at)}
                </div>
                <dl style={{ margin: "0.5rem 0 0" }}>
                  {proposedChanges(proposal).map(([field, value]) => (
                    <div key={field} style={{ display: "flex", gap: 8 }}>
                      <dt style={{ ...ui.muted, minWidth: "8rem" }}>{field}</dt>
                      <dd style={{ margin: 0, fontFamily: "monospace", fontSize: "0.875rem" }}>
                        {value}
                      </dd>
                    </div>
                  ))}
                </dl>
                {proposalNote(proposal) !== "" && (
                  <p style={{ ...ui.muted, marginBottom: 0 }}>note: {proposalNote(proposal)}</p>
                )}
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexShrink: 0 }}>
                <button
                  type="button"
                  style={ui.primaryButton}
                  disabled={busy === proposal.id}
                  onClick={() => void decide(proposal, "approve")}
                >
                  Approve
                </button>
                <button
                  type="button"
                  style={ui.dangerButton}
                  disabled={busy === proposal.id}
                  onClick={() => void decide(proposal, "reject")}
                >
                  Reject
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
