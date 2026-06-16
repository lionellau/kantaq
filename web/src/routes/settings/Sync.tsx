/**
 * E20-T2 (MOD-12) — Settings → Sync: where committed state stands.
 *
 * Read-only and honest. Background push/pull is not wired in this version
 * (MOD-04 lands it next), so there is no "sync now" button to fake — instead
 * the page shows the configured backend mode and the local event-log state:
 * how many events are still local-only versus acknowledged by a backend, and
 * when the last commit landed (none until sync is enabled).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { SyncStatus, WorkspaceMetrics } from "../../api/types";
import MetricsDashboard from "../../components/MetricsDashboard";
import { fmtDateTime } from "../../lib/format";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

const MODE_LABEL: Record<string, string> = {
  local: "Local only — no remote backend",
  supabase: "Supabase — shared team backend",
  postgres: "Self-hosted Postgres",
};

// MOD-26 §B3 / E05-T3 — how a stale agent proposal is handled on sync.
const PROPOSAL_POLICY_LABEL: Record<string, string> = {
  auto_rebase: "Auto-rebase — only re-decide a proposal that genuinely conflicts",
  strict_rebase: "Strict — re-confirm any proposal that raced a change",
};

export default function Sync() {
  const { connected } = useSession();
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [metrics, setMetrics] = useState<WorkspaceMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const [statusRes, metricsRes] = await Promise.all([
      api.GET("/v1/sync/status"),
      api.GET("/v1/metrics/summary", { params: { query: { window_days: 30 } } }),
    ]);
    if (statusRes.error !== undefined) {
      setError("could not load sync status");
      return;
    }
    setError(null);
    setStatus(statusRes.data);
    setMetrics(metricsRes.data ?? null);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!connected) {
    return (
      <section>
        <h1>Sync</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <p style={{ margin: 0 }}>
        <Link to="/settings" style={ui.muted}>
          ← Settings
        </Link>
      </p>
      <h1>Sync</h1>
      <p style={ui.muted}>
        kantaq is local-first: your work is saved on this machine and syncs to the team backend when
        enabled. Background push/pull is not running in this version yet — the counts below show
        what is still local versus committed.
      </p>

      {error !== null && <p style={ui.errorText}>{error}</p>}

      {status !== null && (
        <>
          <div style={ui.card}>
            <p style={{ margin: 0 }}>
              <strong>Backend:</strong> {MODE_LABEL[status.hub_mode] ?? status.hub_mode}
            </p>
            <p style={{ marginBottom: 0 }}>
              <output>
                {status.backend_configured
                  ? "A remote backend is configured."
                  : "No remote backend configured — everything stays on this machine."}
              </output>
            </p>
          </div>

          <h2 style={ui.sectionHeading}>Local event log</h2>
          <table style={ui.table}>
            <tbody>
              <tr>
                <td style={ui.td}>Pending (local only)</td>
                <td style={ui.td} data-testid="sync-pending">
                  {status.pending_events}
                </td>
              </tr>
              <tr>
                <td style={ui.td}>Committed to the backend</td>
                <td style={ui.td}>{status.committed_events}</td>
              </tr>
              <tr>
                <td style={ui.td}>Total events</td>
                <td style={ui.td}>{status.total_events}</td>
              </tr>
              <tr>
                <td style={ui.td}>Last commit</td>
                <td style={ui.td}>{fmtDateTime(status.last_committed_at)}</td>
              </tr>
            </tbody>
          </table>

          <h2 style={ui.sectionHeading}>Conflict handling</h2>
          <div style={ui.card}>
            <p style={{ margin: 0 }}>
              <strong>Stale agent proposals:</strong>{" "}
              <span data-testid="proposal-stale-policy">
                {PROPOSAL_POLICY_LABEL[status.agent_proposal_stale_policy] ??
                  status.agent_proposal_stale_policy}
              </span>
            </p>
            <p style={{ ...ui.muted, marginBottom: 0 }}>
              When you approve an agent's proposal but the ticket moved on since it was made, the
              agent's stale value never silently wins — the proposal comes back for a quick
              re-decision. Set with <code>AGENT_PROPOSAL_STALE_POLICY</code> in your runtime config.
            </p>
          </div>

          {metrics !== null && <MetricsDashboard metrics={metrics} />}
        </>
      )}
    </section>
  );
}
