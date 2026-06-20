/**
 * E13-T6 (MOD-19 / MOD-12) — one memory promotion in the Inbox queue.
 *
 * A teammate (or an agent, via the propose-first `memory_promote` tool) has
 * proposed a memory entry for the team; a human previews the title, body, and
 * provenance and Approves or Rejects it — the same human-gated loop as an agent
 * proposal, calling the v0.2 `/v1/memory/{id}/approve|reject` routes (agents get
 * 403). The body is untrusted text (PRD §15) and renders as plain text, never
 * markup. The promoted entry has no separately-exposed prior team version, so
 * the preview is the proposed content itself — no guessed "before".
 */

import type { MemoryEntry } from "../api/types";
import { fmtDateTime } from "../lib/format";
import * as ui from "../lib/ui";

/** Provenance is a flat string→string map (who/when/how); show its entries. */
export function provenanceEntries(entry: MemoryEntry): [string, string][] {
  return Object.entries(entry.provenance ?? {});
}

export default function MemoryPromotionCard({
  entry,
  busy,
  onDecide,
}: {
  entry: MemoryEntry;
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}) {
  const provenance = provenanceEntries(entry);

  return (
    <li style={ui.card} aria-label={`memory promotion ${entry.id}`}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontWeight: 600 }}>{entry.title}</div>
          <div style={ui.muted}>
            {entry.type} · {entry.space} · confidence {entry.confidence} · proposed by{" "}
            {entry.created_by ?? "unknown"} · {fmtDateTime(entry.updated_at)}
          </div>

          {entry.body.trim() !== "" && (
            <p style={{ margin: "0.6rem 0 0", whiteSpace: "pre-wrap" }}>{entry.body}</p>
          )}

          {provenance.length > 0 && (
            <dl style={{ ...ui.muted, margin: "0.6rem 0 0", display: "grid", gap: 2 }}>
              {provenance.map(([key, value]) => (
                <div key={key} style={{ display: "flex", gap: 6 }}>
                  <dt style={{ fontWeight: 600 }}>{key}:</dt>
                  <dd style={{ margin: 0, minWidth: 0, overflowWrap: "anywhere" }}>{value}</dd>
                </div>
              ))}
            </dl>
          )}
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
