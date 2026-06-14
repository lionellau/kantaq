/**
 * E20-T3 (MOD-12) — the Agents page: the honest, audit-driven trust surface.
 *
 * "This page is the audit and trust surface. It must be honest and complete:
 * every active session is here; every denied call is here." (PRD §16.7,
 * NFR-E20-1.) A v0.1 session is a capability grant whose subject is an agent
 * (MOD-08). The list comes from `/v1/agents/sessions` (the signed grants — the
 * cross-process source of truth, since the gateway's live sessions live in its
 * own process); each session's recent + denied calls come from `/v1/audit/range`
 * — read **live** every 2 s poll, never cached, so a denial appears the instant
 * the gateway writes it.
 *
 * The two controls are real, audited writes (no no-op buttons, MOD-12 honesty
 * rule): Revoke calls `POST /v1/grants/{id}/revoke`, which kills the derived
 * session on its next call (within the revocation budget, NFR-E06-2); Rotate
 * calls `POST /v1/members/{id}/rotate` and surfaces the new token once.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { AgentSession, AuditCall } from "../api/types";
import MintedToken from "../components/MintedToken";
import SessionCard from "../components/SessionCard";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

/** Group audit calls by the actor that made them (the session owner). */
function byActor(calls: AuditCall[]): Record<string, AuditCall[]> {
  const grouped: Record<string, AuditCall[]> = {};
  for (const call of calls) {
    const bucket = grouped[call.actor_id] ?? [];
    bucket.push(call);
    grouped[call.actor_id] = bucket;
  }
  return grouped;
}

export default function Agents() {
  const { connected } = useSession();
  const [sessions, setSessions] = useState<AgentSession[] | null>(null);
  const [calls, setCalls] = useState<Record<string, AuditCall[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notices, setNotices] = useState<Record<string, string>>({});
  const [minted, setMinted] = useState<{ label: string; token: string } | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const [sessionsRes, callsRes] = await Promise.all([
      api.GET("/v1/agents/sessions"),
      api.GET("/v1/audit/range", { params: { query: { source: "mcp", limit: 200 } } }),
    ]);
    if (sessionsRes.error !== undefined) {
      setError("could not load agent sessions");
      return;
    }
    setError(null);
    setSessions(sessionsRes.data);
    setCalls(byActor(callsRes.data ?? []));
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(refresh, 2000, connected);

  async function revoke(session: AgentSession) {
    setBusy(session.grant_id);
    const { error: apiError } = await api.POST("/v1/grants/{grant_id}/revoke", {
      params: { path: { grant_id: session.grant_id } },
    });
    setBusy(null);
    setNotices((prev) => ({
      ...prev,
      [session.grant_id]: apiError
        ? "could not revoke the grant"
        : "Grant revoked — the session stops on its next call.",
    }));
    void refresh();
  }

  async function rotate(session: AgentSession) {
    setBusy(session.grant_id);
    const { data, error: apiError } = await api.POST("/v1/members/{member_id}/rotate", {
      params: { path: { member_id: session.owner_member_id } },
    });
    setBusy(null);
    if (apiError !== undefined || data === undefined) {
      setNotices((prev) => ({ ...prev, [session.grant_id]: "could not rotate the token" }));
      return;
    }
    setMinted({
      label: `New token for ${session.owner_email ?? session.owner_member_id}`,
      token: data.token,
    });
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Agents</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1>Agents</h1>
      <p style={ui.muted}>
        Every agent session and every denied call, live from the audit log. Revoke a grant to stop
        an agent; rotate its token to re-key it.
      </p>
      {error !== null && <p style={ui.errorText}>{error}</p>}
      {minted !== null && (
        <div style={{ margin: "0.75rem 0" }}>
          <MintedToken
            label={minted.label}
            token={minted.token}
            onDismiss={() => setMinted(null)}
          />
        </div>
      )}
      {sessions !== null && sessions.length === 0 && (
        <p style={ui.muted}>
          No agent sessions here. Invite an agent and issue it a capability grant from{" "}
          <Link to="/settings/members">Members</Link>. Workspace-wide agent oversight requires
          credential-admin (Maintainer+) rights.
        </p>
      )}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 12 }}>
        {sessions?.map((session) => (
          <SessionCard
            key={session.grant_id}
            session={session}
            calls={calls[session.owner_member_id] ?? []}
            busy={busy === session.grant_id}
            notice={notices[session.grant_id] ?? null}
            onRevoke={() => void revoke(session)}
            onRotate={() => void rotate(session)}
          />
        ))}
      </ul>
    </section>
  );
}
