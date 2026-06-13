/**
 * E20-T3 (MOD-12) — one agent session on the Agents page.
 *
 * A session is a capability grant whose subject is an agent (MOD-08). The card
 * is the honest, complete per-session record (PRD §16.7): owner, grant, scope,
 * granted capabilities (verbs), write mode, expiry, live active/revoked state,
 * and the session's recent + denied calls (read live from audit). The two
 * controls are real, audited writes — revoke the grant (which kills the derived
 * session within the revocation budget, NFR-E06-2) and rotate the owner's token
 * — so the surface never offers a button that does nothing (MOD-12 honesty
 * rule). A revoked session keeps its row, marked, rather than vanishing.
 */

import type { AgentSession, AuditCall } from "../api/types";
import { fmtEpoch } from "../lib/format";
import * as ui from "../lib/ui";
import CallList from "./CallList";

export default function SessionCard({
  session,
  calls,
  busy,
  notice,
  onRevoke,
  onRotate,
}: {
  session: AgentSession;
  calls: AuditCall[];
  busy: boolean;
  notice: string | null;
  onRevoke: () => void;
  onRotate: () => void;
}) {
  const denied = calls.filter((c) => c.reason !== null);
  return (
    <li
      style={{ ...ui.card, opacity: session.active ? 1 : 0.7 }}
      aria-label={`agent session ${session.grant_id}`}
      data-active={session.active}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600 }}>
              {session.owner_email ?? session.owner_member_id}
            </span>
            <span style={ui.chip}>{session.owner_role ?? "subject unknown"}</span>
            {session.active ? (
              <span
                style={{
                  ...ui.chip,
                  background: "#dcfce7",
                  color: "#166534",
                  borderColor: "#dcfce7",
                }}
              >
                active
              </span>
            ) : (
              <span
                style={{
                  ...ui.chip,
                  background: "#fde2e1",
                  color: ui.palette.danger,
                  borderColor: "#fde2e1",
                }}
              >
                {session.reason === "revoked" ? "revoked" : `inactive: ${session.reason}`}
              </span>
            )}
          </div>
          <div style={{ ...ui.muted, fontSize: "0.75rem", marginTop: 2 }}>
            grant {session.grant_id}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexShrink: 0 }}>
          <button
            type="button"
            style={ui.button}
            disabled={busy}
            onClick={onRotate}
            title="Revoke this member's token and mint a fresh one"
          >
            Rotate token
          </button>
          <button
            type="button"
            style={ui.dangerButton}
            disabled={busy || !session.active}
            onClick={onRevoke}
            title="Revoke the grant; the session stops on its next call"
          >
            Revoke
          </button>
        </div>
      </div>

      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "2px 12px",
          margin: "0.6rem 0 0",
        }}
      >
        <dt style={ui.label}>scope</dt>
        <dd style={{ margin: 0, fontSize: "0.875rem" }}>{session.resource}</dd>
        <dt style={ui.label}>tools</dt>
        <dd style={{ margin: 0, fontSize: "0.875rem" }}>
          {session.verbs.length === 0 ? (
            <span style={ui.muted}>none</span>
          ) : (
            <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
              {session.verbs.map((verb) => (
                <span key={verb} style={ui.chip}>
                  {verb}
                </span>
              ))}
            </span>
          )}
        </dd>
        <dt style={ui.label}>write mode</dt>
        <dd style={{ margin: 0, fontSize: "0.875rem" }}>
          {session.write_mode === "propose_only" ? "propose-only" : "read-only"}
        </dd>
        <dt style={ui.label}>expires</dt>
        <dd style={{ margin: 0, fontSize: "0.875rem" }}>{fmtEpoch(session.expires_at)}</dd>
      </dl>

      {notice !== null && (
        <p style={{ margin: "0.5rem 0 0" }}>
          <output>{notice}</output>
        </p>
      )}

      <div style={{ marginTop: "0.75rem" }}>
        <div style={ui.label}>Recent calls {denied.length > 0 && `· ${denied.length} denied`}</div>
        <div style={{ marginTop: 4 }}>
          <CallList calls={calls} emptyText="No calls recorded for this agent yet." />
        </div>
      </div>
    </li>
  );
}
