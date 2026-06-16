/**
 * E20-T1/T3/T4 (MOD-12) — the Inbox: the human review queue.
 *
 * Three tabs (FR-E20-2): **proposals** (pending agent writes, shown as a
 * field-level diff against the live ticket plus the memory cited for that
 * ticket), **memory promotions** (an empty state — the approve/reject UI is a
 * deferred follow-up, MOD-19 / DEBT-28; the backend loop is API-complete), and
 * **denied calls** (recent gateway denials, read live from audit). A count
 * badge rides each tab; when no proposal is pending the proposals tab shows the
 * Inbox-zero state.
 *
 * Approve applies the proposed change through the runtime's one write path and
 * flips the proposal; Reject declines it; a 409 means someone decided first.
 * Proposed values are untrusted agent text — rendered as plain text via
 * FieldDiff, never markup (PRD §15).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { AuditCall, LinkedMemory, Proposal, Ticket } from "../api/types";
import CallList from "../components/CallList";
import ProposalCard from "../components/ProposalCard";
import Tabs from "../components/Tabs";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

type TabId = "proposals" | "memory" | "denied";

export default function Inbox() {
  const { connected } = useSession();
  const [tab, setTab] = useState<TabId>("proposals");
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const [tickets, setTickets] = useState<Record<string, Ticket>>({});
  const [memory, setMemory] = useState<Record<string, LinkedMemory[]>>({});
  const [denied, setDenied] = useState<AuditCall[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const [proposalsRes, deniedRes] = await Promise.all([
      api.GET("/v1/proposals", { params: { query: { status: "pending" } } }),
      api.GET("/v1/audit/range", {
        params: { query: { action: "tool.deny", source: "mcp", limit: 200 } },
      }),
    ]);
    if (proposalsRes.error !== undefined) {
      setError("could not load the queue");
      return;
    }
    setError(null);
    const pending = proposalsRes.data ?? [];
    setProposals(pending);
    setDenied(deniedRes.data ?? []);

    // Fetch each proposal's ticket (for the before-values) and its cited memory.
    const ticketIds = [...new Set(pending.map((p) => p.ticket_id))];
    const loaded = await Promise.all(
      ticketIds.map(async (id) => {
        const params = { params: { path: { ticket_id: id } } };
        const [ticketRes, memoryRes] = await Promise.all([
          api.GET("/v1/tickets/{ticket_id}", params),
          api.GET("/v1/tickets/{ticket_id}/memory", params),
        ]);
        return { id, ticket: ticketRes.data ?? null, memory: memoryRes.data ?? [] };
      }),
    );
    const ticketMap: Record<string, Ticket> = {};
    const memoryMap: Record<string, LinkedMemory[]> = {};
    for (const { id, ticket, memory: mem } of loaded) {
      if (ticket !== null) {
        ticketMap[id] = ticket;
      }
      memoryMap[id] = mem;
    }
    setTickets(ticketMap);
    setMemory(memoryMap);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(refresh, 2000, connected);

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

  const pendingCount = proposals?.length ?? 0;

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

      <Tabs
        tabs={[
          { id: "proposals", label: "Proposals", count: pendingCount },
          { id: "memory", label: "Memory promotions" },
          { id: "denied", label: "Denied calls", count: denied.length },
        ]}
        active={tab}
        onSelect={(id) => setTab(id as TabId)}
      >
        {tab === "proposals" &&
          (proposals !== null && pendingCount === 0 ? (
            <p style={ui.muted}>Inbox zero — no proposals waiting. 🎉</p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 12 }}>
              {proposals?.map((proposal) => (
                <ProposalCard
                  key={proposal.id}
                  proposal={proposal}
                  ticket={tickets[proposal.ticket_id] ?? null}
                  citedMemory={memory[proposal.ticket_id] ?? []}
                  busy={busy === proposal.id}
                  onDecide={(decision) => void decide(proposal, decision)}
                />
              ))}
            </ul>
          ))}

        {tab === "memory" && (
          <p style={ui.muted}>
            No memory promotions yet. Agents will propose memory entries to share here in a later
            release (v0.2).
          </p>
        )}

        {tab === "denied" && (
          <CallList
            calls={denied}
            emptyText="No denied calls. The gateway has blocked nothing recently."
          />
        )}
      </Tabs>
    </section>
  );
}
