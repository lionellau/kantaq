/**
 * E20-T3 (MOD-12) — the memory cited alongside a proposal in the Inbox.
 *
 * Per PRD §16.6 and the architecture's approval flow, the human deciding a
 * proposal sees "the diff and the cited memory": the context entries linked to
 * the proposal's ticket — what the agent's role context could read (MOD-19/21).
 * Compact on purpose (title + type + why-linked + provenance); the full bodies
 * live one click away on the ticket page. Memory titles are entity text but not
 * markdown here — rendered as plain text, the queue stays scannable and inert.
 */

import { Link } from "react-router-dom";
import type { LinkedMemory } from "../api/types";
import { fmtDateTime } from "../lib/format";
import * as ui from "../lib/ui";

export default function CitedMemory({
  items,
  ticketId,
}: {
  items: LinkedMemory[];
  ticketId: string;
}) {
  return (
    <div style={{ marginTop: "0.75rem" }}>
      <div style={ui.label}>Cited memory</div>
      {items.length === 0 ? (
        <p style={{ ...ui.muted, margin: "2px 0 0" }}>
          None linked to{" "}
          <Link to={`/tickets/${ticketId}`} style={ui.muted}>
            this ticket
          </Link>
          .
        </p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "4px 0 0", display: "grid", gap: 6 }}>
          {items.map(({ link, entry }) => (
            <li
              key={link.id}
              style={{
                borderLeft: `2px solid ${ui.palette.border}`,
                paddingLeft: 8,
                fontSize: "0.8125rem",
              }}
            >
              <div style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}>
                <span style={{ fontWeight: 600 }}>{entry.title}</span>
                <span style={ui.chip}>{entry.type}</span>
              </div>
              <div style={{ ...ui.muted, fontSize: "0.75rem" }}>
                linked because: {link.reason} · from {entry.provenance.origin ?? entry.source} by{" "}
                {entry.provenance.actor_id ?? entry.created_by ?? "unknown"}
                {entry.provenance.captured_at !== undefined &&
                  ` · ${fmtDateTime(entry.provenance.captured_at)}`}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
