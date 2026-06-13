/**
 * E20-T3/T4 (MOD-12) — a list of audit calls (`/v1/audit/range` rows).
 *
 * Shared by the Agents page (a session's recent + denied calls) and the Inbox
 * denied-calls tab. A denial carries a `reason` (the failed gateway check) and
 * `detail`, both shown; an allowed call shows its action + target. Everything
 * is server-recorded audit data rendered as plain text — the trust surface
 * never reinterprets it.
 */

import type { AuditCall } from "../api/types";
import { fmtDateTime } from "../lib/format";
import * as ui from "../lib/ui";

/** "tools/ticket_search" → "ticket_search"; passes other refs through. */
function targetLabel(call: AuditCall): string {
  if (call.object_ref === null) {
    return call.action;
  }
  return call.object_ref.startsWith("tools/")
    ? call.object_ref.slice("tools/".length)
    : call.object_ref;
}

export default function CallList({
  calls,
  emptyText = "No calls.",
}: {
  calls: AuditCall[];
  emptyText?: string;
}) {
  if (calls.length === 0) {
    return <p style={{ ...ui.muted, margin: 0 }}>{emptyText}</p>;
  }
  return (
    <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 6 }}>
      {calls.map((call) => {
        const denied = call.reason !== null;
        return (
          <li
            key={call.id}
            style={{ display: "grid", gap: 2, fontSize: "0.8125rem" }}
            data-denied={denied}
          >
            <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <span style={{ fontFamily: "monospace", fontWeight: 600 }}>{targetLabel(call)}</span>
              {denied ? (
                <span
                  style={{
                    ...ui.chip,
                    background: "#fde2e1",
                    color: ui.palette.danger,
                    borderColor: "#fde2e1",
                  }}
                >
                  denied: {call.reason}
                </span>
              ) : (
                <span style={ui.chip}>{call.action}</span>
              )}
              <span style={{ ...ui.muted, fontSize: "0.75rem" }}>
                {fmtDateTime(call.created_at)}
              </span>
            </div>
            {denied && call.detail !== null && (
              <span style={{ ...ui.muted, fontSize: "0.75rem" }}>{call.detail}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
