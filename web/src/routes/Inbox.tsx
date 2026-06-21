/**
 * E20-T1/T3/T4 (MOD-12) + E13-T6 (MOD-19) — the Inbox: the human review queue.
 *
 * Four tabs (FR-E20-2): **proposals** (pending agent writes, shown as a
 * field-level diff against the live ticket plus the memory cited for that
 * ticket), **sync conflicts** (open field collisions), **memory promotions**
 * (entries proposed for the team — preview + Approve/Reject over the v0.2
 * `/v1/memory/{id}/approve|reject` routes, the GUI for the API loop that closes
 * DEBT-28), and **denied calls** (recent gateway denials, read live from
 * audit). A count badge rides each tab; when a queue is empty it shows its
 * inbox-zero state.
 *
 * Approve applies the proposed change through the runtime's one write path and
 * flips the proposal; Reject declines it; a 409 means someone decided first.
 * Memory Approve/Reject are human-only (agents 403 at the route). Proposed
 * values are untrusted agent text — rendered as plain text (via FieldDiff for
 * tickets, plain text for memory bodies), never markup (PRD §15).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type {
  AuditCall,
  Conflict,
  LinkedMemory,
  MemoryEntry,
  Proposal,
  Ticket,
  TicketPatch,
} from "../api/types";
import CallList from "../components/CallList";
import ConflictCard from "../components/ConflictCard";
import { displayValue } from "../components/FieldDiff";
import MemoryPromotionCard from "../components/MemoryPromotionCard";
import ProposalCard, { proposedChanges } from "../components/ProposalCard";
import Tabs from "../components/Tabs";
import { useMemberDirectory } from "../lib/members";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

/** A just-approved proposal we can still revert (E20-T6 Undo): the fields it
 *  changed, with the live value captured before the apply. */
interface ApprovedUndo {
  proposalId: string;
  ticketId: string;
  ticketTitle: string;
  fields: { field: string; before: unknown; after: unknown }[];
}

type TabId = "proposals" | "conflicts" | "memory" | "denied";

export default function Inbox() {
  const { connected } = useSession();
  const directory = useMemberDirectory(connected);
  const [tab, setTab] = useState<TabId>("proposals");
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const [tickets, setTickets] = useState<Record<string, Ticket>>({});
  const [memory, setMemory] = useState<Record<string, LinkedMemory[]>>({});
  const [denied, setDenied] = useState<AuditCall[]>([]);
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [promotions, setPromotions] = useState<MemoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [approved, setApproved] = useState<ApprovedUndo | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const [proposalsRes, deniedRes, conflictsRes, promotionsRes] = await Promise.all([
      api.GET("/v1/proposals", { params: { query: { status: "pending" } } }),
      api.GET("/v1/audit/range", {
        params: { query: { action: "tool.deny", source: "mcp", limit: 200 } },
      }),
      api.GET("/v1/conflicts", { params: { query: { status: "open" } } }),
      api.GET("/v1/memory", { params: { query: { review_status: "proposed" } } }),
    ]);
    if (proposalsRes.error !== undefined) {
      setError("could not load the queue");
      return;
    }
    setError(null);
    const pending = proposalsRes.data ?? [];
    setProposals(pending);
    setDenied(deniedRes.data ?? []);
    setConflicts(conflictsRes.data ?? []);
    setPromotions(promotionsRes.data ?? []);

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

  async function decide(proposal: Proposal, decision: "approve" | "reject", reason?: string) {
    setBusy(proposal.id);
    setNotice(null);
    const path = { params: { path: { proposal_id: proposal.id } } };

    if (decision === "approve") {
      const { response, error: apiError } = await api.POST(
        "/v1/proposals/{proposal_id}/approve",
        path,
      );
      setBusy(null);
      if (apiError !== undefined) {
        setNotice(
          response?.status === 409
            ? "that proposal was already decided elsewhere"
            : "could not approve the proposal",
        );
      } else {
        // Stage an Undo: the fields the proposal changed, with the live value
        // captured *before* the apply, so we can revert through the ticket path.
        const live = (tickets[proposal.ticket_id] ?? {}) as Record<string, unknown>;
        setApproved({
          proposalId: proposal.id,
          ticketId: proposal.ticket_id,
          ticketTitle: proposal.ticket_title ?? proposal.ticket_id,
          fields: proposedChanges(proposal).map(([field, after]) => ({
            field,
            before: live[field],
            after,
          })),
        });
      }
      void refresh();
      return;
    }

    const { response, error: apiError } = await api.POST("/v1/proposals/{proposal_id}/reject", {
      ...path,
      body: { reason: reason ?? null },
    });
    setBusy(null);
    if (apiError !== undefined) {
      setNotice(
        response?.status === 409
          ? "that proposal was already decided elsewhere"
          : "could not reject the proposal",
      );
    } else {
      setNotice(
        reason !== undefined ? "Rejected — your reason reaches the agent's owner." : "Rejected.",
      );
    }
    void refresh();
  }

  // E20-T9: nudge the approver that a pending proposal needs a decision — a
  // content-free signal to the workspace sink (no-op if none is configured).
  async function notify(proposal: Proposal) {
    setBusy(proposal.id);
    setNotice(null);
    const { response, error: apiError } = await api.POST("/v1/proposals/{proposal_id}/notify", {
      params: { path: { proposal_id: proposal.id } },
    });
    setBusy(null);
    if (apiError !== undefined) {
      setNotice(
        response?.status === 409
          ? "that proposal was already decided"
          : response?.status === 403
            ? "only a workspace member may notify the approver"
            : "could not send the nudge (is a notification sink configured?)",
      );
    } else {
      setNotice("Nudge sent — the approver's sink was pinged (if one is configured).");
    }
  }

  // Undo a just-approved proposal: revert the changed fields to their captured
  // pre-approve values through the one ticket write path (audited like any edit).
  async function undo(record: ApprovedUndo) {
    setBusy(record.proposalId);
    const revert: Record<string, unknown> = {};
    for (const { field, before } of record.fields) {
      if (before !== undefined) {
        revert[field] = before;
      }
    }
    const { error: apiError } = await api.PATCH("/v1/tickets/{ticket_id}", {
      params: { path: { ticket_id: record.ticketId } },
      body: revert as TicketPatch,
    });
    setBusy(null);
    if (apiError !== undefined) {
      setNotice("could not undo — the ticket may have moved on; edit it directly.");
    } else {
      setApproved(null);
      setNotice("Undone — the ticket is back to its previous values.");
    }
    void refresh();
  }

  // Memory promotions reuse the same human-gated loop as proposals, but call the
  // v0.2 `/v1/memory/{id}/approve|reject` routes (human-only — an agent gets 403
  // at the route, never here). Approve flips the entry to `team`/`approved` and
  // it syncs; a 409 means someone decided it first.
  async function decideMemory(entry: MemoryEntry, decision: "approve" | "reject") {
    setBusy(entry.id);
    setNotice(null);
    const path = { params: { path: { memory_id: entry.id } } };
    const { response, error: apiError } =
      decision === "approve"
        ? await api.POST("/v1/memory/{memory_id}/approve", path)
        : await api.POST("/v1/memory/{memory_id}/reject", path);
    setBusy(null);
    if (apiError !== undefined) {
      setNotice(
        response?.status === 409
          ? "that promotion was already decided elsewhere"
          : `could not ${decision} the promotion`,
      );
    } else {
      setNotice(
        decision === "approve" ? "Approved — the entry is now shared with the team." : "Rejected.",
      );
    }
    void refresh();
  }

  async function resolve(
    conflict: Conflict,
    choice: "keep-A" | "keep-B" | "new-value",
    newValue?: string,
  ) {
    setBusy(conflict.id);
    setNotice(null);
    const {
      data,
      response,
      error: apiError,
    } = await api.POST("/v1/conflicts/{conflict_id}/resolve", {
      params: { path: { conflict_id: conflict.id } },
      body: { choice, new_value: newValue ?? null },
    });
    setBusy(null);
    if (apiError !== undefined) {
      setNotice(
        response?.status === 409
          ? "conflict resolution needs the shared backend — sign in and sync first"
          : "could not resolve the conflict",
      );
    } else if (data?.rebase_required === true) {
      // The field moved since this record was minted — nothing applied; re-decide.
      setNotice("The field changed since this conflict — re-decide against the current value.");
    } else {
      setNotice("Resolved — your choice is recorded and synced.");
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

      {approved !== null && (
        <output
          style={{
            ...ui.card,
            display: "block",
            borderColor: ui.palette.accent,
            marginBottom: 12,
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <div style={{ minWidth: 0 }}>
              <strong>Approved</strong> —{" "}
              <Link to={`/tickets/${approved.ticketId}`}>{approved.ticketTitle}</Link> is updated:
              <ul style={{ margin: "0.4rem 0 0", paddingLeft: "1.2rem" }}>
                {approved.fields.map((f) => (
                  <li key={f.field} style={ui.muted}>
                    {f.field}: <code>{displayValue(f.after)}</code>
                  </li>
                ))}
              </ul>
            </div>
            <div style={{ display: "flex", gap: 8, flexShrink: 0, alignItems: "flex-start" }}>
              <button
                type="button"
                style={ui.button}
                disabled={busy === approved.proposalId}
                onClick={() => void undo(approved)}
              >
                Undo
              </button>
              <button type="button" style={ui.button} onClick={() => setApproved(null)}>
                Dismiss
              </button>
            </div>
          </div>
        </output>
      )}

      <Tabs
        tabs={[
          { id: "proposals", label: "Proposals", count: pendingCount },
          { id: "conflicts", label: "Sync conflicts", count: conflicts.length },
          { id: "memory", label: "Memory promotions", count: promotions?.length ?? 0 },
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
                  directory={directory}
                  busy={busy === proposal.id}
                  onDecide={(decision, reason) => void decide(proposal, decision, reason)}
                  onNotify={() => void notify(proposal)}
                />
              ))}
            </ul>
          ))}

        {tab === "conflicts" &&
          (conflicts.length === 0 ? (
            <p style={ui.muted}>No sync conflicts — every field converged cleanly. 🎉</p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 12 }}>
              {conflicts.map((conflict) => (
                <ConflictCard
                  key={conflict.id}
                  conflict={conflict}
                  directory={directory}
                  busy={busy === conflict.id}
                  onResolve={(choice, newValue) => void resolve(conflict, choice, newValue)}
                />
              ))}
            </ul>
          ))}

        {tab === "memory" &&
          (promotions !== null && promotions.length === 0 ? (
            <p style={ui.muted}>
              No memory promotions waiting. Promote a draft from the{" "}
              <Link to="/memory">Memory</Link> page to share it with the team. 🎉
            </p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 12 }}>
              {promotions?.map((entry) => (
                <MemoryPromotionCard
                  key={entry.id}
                  entry={entry}
                  directory={directory}
                  busy={busy === entry.id}
                  onDecide={(decision) => void decideMemory(entry, decision)}
                />
              ))}
            </ul>
          ))}

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
