/**
 * E13-T3 (MOD-19/MOD-11) — the Memory page.
 *
 * One filterable table over `/v1/memory` (space, type, keyword search), a
 * compact create form, and a per-row "link to ticket" composer. Visibility is
 * chosen at create only (it is immutable in v0.1): `private_local` entries
 * never leave this machine (NFR-E13-1) and wear a "private" badge. Refreshes
 * on the 2 s poll (MOD-14) like the rest of the shell.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import {
  MEMORY_CONFIDENCE,
  MEMORY_SPACES,
  MEMORY_TYPES,
  type MemoryEntry,
  type Ticket,
} from "../api/types";
import { fmtDateTime } from "../lib/format";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

interface Filters {
  space: string;
  type: string;
  q: string;
}

const NO_FILTERS: Filters = { space: "", type: "", q: "" };

/** The privacy badge: local entries say so, loudly but compactly. */
export function VisibilityBadge({ entry }: { entry: MemoryEntry }) {
  if (entry.visibility === "local") {
    return (
      <span
        aria-label="visibility: private to this machine"
        style={{ ...ui.chip, background: ui.palette.warnBg, color: ui.palette.warnText }}
      >
        private
      </span>
    );
  }
  return (
    <span aria-label={`visibility: ${entry.domain_visibility}`} style={ui.chip}>
      team
    </span>
  );
}

/**
 * Can this entry be promoted to a team proposal? (MOD-19 lifecycle, E13-T6)
 * A `local` entry promotes to a new team/`proposed` copy (the local stays
 * private, NFR-E13-1); a `team` entry in `draft`/`stale` promotes in place.
 * Anything already `proposed`/`approved`/`rejected` is not promotable.
 */
export function isPromotable(entry: MemoryEntry): boolean {
  if (entry.visibility === "local") {
    return true;
  }
  return entry.review_status === "draft" || entry.review_status === "stale";
}

export default function Memory() {
  const { connected } = useSession();
  const [entries, setEntries] = useState<MemoryEntry[] | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [linkingId, setLinkingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const query: Record<string, string> = {};
    if (filters.space) query.space = filters.space;
    if (filters.type) query.type = filters.type;
    if (filters.q) query.q = filters.q;
    const { data, error: apiError } = await api.GET("/v1/memory", { params: { query } });
    if (apiError !== undefined) {
      setError("could not load memory entries");
      return;
    }
    setError(null);
    setEntries(data);
  }, [connected, filters]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(refresh, 2000, connected);

  useEffect(() => {
    if (!connected) {
      return;
    }
    void api.GET("/v1/tickets").then(({ data }) => setTickets(data ?? []));
  }, [connected]);

  function setFilter(key: keyof Filters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  // "Promote to team" — route a local/draft entry into the Inbox approval queue
  // (POST /v1/memory/{id}/promote, human-only, memory.write). Nothing is shared
  // until a human approves the resulting `proposed` entry in the Inbox.
  async function promote(entry: MemoryEntry) {
    setNotice(null);
    const { response, error: apiError } = await api.POST("/v1/memory/{memory_id}/promote", {
      params: { path: { memory_id: entry.id } },
    });
    if (apiError !== undefined) {
      setError(
        response?.status === 422
          ? "this entry can't be promoted from its current state"
          : "could not promote the entry",
      );
      return;
    }
    setError(null);
    setNotice("Promoted — it's now awaiting approval in the Inbox.");
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Memory</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1>Memory</h1>
      <p style={ui.muted}>
        Scoped context next to the work. Private entries never leave this machine.
      </p>

      <form aria-label="Memory filters" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <label style={ui.label}>
          Space
          <select
            style={ui.input}
            value={filters.space}
            onChange={(event) => setFilter("space", event.target.value)}
          >
            <option value="">All</option>
            {MEMORY_SPACES.map((space) => (
              <option key={space} value={space}>
                {space}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Type
          <select
            style={ui.input}
            value={filters.type}
            onChange={(event) => setFilter("type", event.target.value)}
          >
            <option value="">All</option>
            {MEMORY_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Search
          <input
            style={ui.input}
            value={filters.q}
            onChange={(event) => setFilter("q", event.target.value)}
            placeholder="title or body"
          />
        </label>
      </form>

      <CreateMemory onCreated={() => void refresh()} />

      {error !== null && <p style={ui.errorText}>{error}</p>}
      {notice !== null && (
        <p>
          <output>{notice}</output>
        </p>
      )}
      {entries !== null && entries.length === 0 && <p style={ui.muted}>No memory entries.</p>}
      {entries !== null && entries.length > 0 && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.th}>Title</th>
              <th style={ui.th}>Type</th>
              <th style={ui.th}>Space</th>
              <th style={ui.th}>Visibility</th>
              <th style={ui.th}>Confidence</th>
              <th style={ui.th}>Updated</th>
              <th style={ui.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td style={ui.td}>
                  <span style={{ fontWeight: 600 }}>{entry.title}</span>
                  {entry.body.trim() !== "" && (
                    <div style={{ ...ui.muted, maxWidth: "28rem" }}>
                      {entry.body.length > 140 ? `${entry.body.slice(0, 140)}…` : entry.body}
                    </div>
                  )}
                </td>
                <td style={ui.td}>{entry.type}</td>
                <td style={ui.td}>{entry.space}</td>
                <td style={ui.td}>
                  <VisibilityBadge entry={entry} />
                </td>
                <td style={ui.td}>{entry.confidence}</td>
                <td style={ui.td}>{fmtDateTime(entry.updated_at)}</td>
                <td style={{ ...ui.td, whiteSpace: "nowrap" }}>
                  <button
                    type="button"
                    style={ui.button}
                    onClick={() => setLinkingId(linkingId === entry.id ? null : entry.id)}
                  >
                    Link to ticket
                  </button>{" "}
                  {isPromotable(entry) && (
                    <button type="button" style={ui.button} onClick={() => void promote(entry)}>
                      Promote to team
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {linkingId !== null && (
        <LinkComposer
          memoryId={linkingId}
          tickets={tickets}
          onLinked={() => {
            setLinkingId(null);
            void refresh();
          }}
        />
      )}
    </section>
  );
}

function CreateMemory({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [type, setType] = useState("note");
  const [space, setSpace] = useState("workspace");
  const [visibility, setVisibility] = useState("team");
  const [confidence, setConfidence] = useState("medium");
  const [error, setError] = useState<string | null>(null);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!title.trim()) {
      setError("a memory entry needs a title");
      return;
    }
    const { error: apiError } = await api.POST("/v1/memory", {
      body: {
        title: title.trim(),
        body,
        type,
        source: "manual",
        space,
        visibility,
        confidence,
        linked_entities: [],
        provenance: {},
        expires_at: null,
      },
    });
    if (apiError !== undefined) {
      setError("could not create the memory entry");
      return;
    }
    setError(null);
    setTitle("");
    setBody("");
    onCreated();
  }

  return (
    <form
      aria-label="Create memory entry"
      onSubmit={create}
      style={{ display: "flex", gap: 8, alignItems: "end", margin: "1rem 0", flexWrap: "wrap" }}
    >
      <label style={ui.label}>
        Title
        <input
          style={{ ...ui.input, minWidth: "14rem" }}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="what to remember"
        />
      </label>
      <label style={ui.label}>
        Body
        <input
          style={{ ...ui.input, minWidth: "16rem" }}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          placeholder="the detail (markdown)"
        />
      </label>
      <label style={ui.label}>
        Type
        <select style={ui.input} value={type} onChange={(event) => setType(event.target.value)}>
          {MEMORY_TYPES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label style={ui.label}>
        Space
        <select style={ui.input} value={space} onChange={(event) => setSpace(event.target.value)}>
          {MEMORY_SPACES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label style={ui.label}>
        Visibility
        <select
          style={ui.input}
          value={visibility}
          onChange={(event) => setVisibility(event.target.value)}
        >
          <option value="team">team (syncs)</option>
          <option value="local">private (never leaves this machine)</option>
        </select>
      </label>
      <label style={ui.label}>
        Confidence
        <select
          style={ui.input}
          value={confidence}
          onChange={(event) => setConfidence(event.target.value)}
        >
          {MEMORY_CONFIDENCE.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" style={ui.primaryButton}>
        Create
      </button>
      {error !== null && <span style={ui.errorText}>{error}</span>}
    </form>
  );
}

function LinkComposer({
  memoryId,
  tickets,
  onLinked,
}: {
  memoryId: string;
  tickets: Ticket[];
  onLinked: () => void;
}) {
  const [ticketId, setTicketId] = useState(tickets[0]?.id ?? "");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function link(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ticketId || !reason.trim()) {
      setError("pick a ticket and give a reason");
      return;
    }
    const { error: apiError } = await api.POST("/v1/memory/{memory_id}/link", {
      params: { path: { memory_id: memoryId } },
      body: { ticket_id: ticketId, reason: reason.trim() },
    });
    if (apiError !== undefined) {
      setError("could not link (already linked?)");
      return;
    }
    setError(null);
    onLinked();
  }

  return (
    <form
      aria-label="Link memory to ticket"
      onSubmit={link}
      style={{ display: "flex", gap: 8, alignItems: "end", margin: "1rem 0", flexWrap: "wrap" }}
    >
      <label style={ui.label}>
        Ticket
        <select
          style={ui.input}
          value={ticketId}
          onChange={(event) => setTicketId(event.target.value)}
        >
          {tickets.map((ticket) => (
            <option key={ticket.id} value={ticket.id}>
              {ticket.title}
            </option>
          ))}
        </select>
      </label>
      <label style={ui.label}>
        Reason
        <input
          style={{ ...ui.input, minWidth: "16rem" }}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="why this context matters"
        />
      </label>
      <button type="submit" style={ui.primaryButton}>
        Link
      </button>
      {error !== null && <span style={ui.errorText}>{error}</span>}
    </form>
  );
}
