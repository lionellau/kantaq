/**
 * E20-T5 (MOD-27 / MOD-12) — the workspace-metrics dashboard for Settings → Sync.
 *
 * Capacity, not cost (D-16): a non-dollar gauge of the shared backend against
 * the Supabase Free 500 MB / 5 GB ceilings, the replica size by project, the
 * per-actor agent-observability table, and retention status. Instead of a
 * projected bill it links into the Supabase console ("View billing ↗"). The
 * dollar economics live in the docs, never as a live number here.
 */

import type { WorkspaceMetrics } from "../api/types";
import { fmtDateTime } from "../lib/format";
import * as ui from "../lib/ui";

function fmtBytes(n: number): string {
  if (n >= 1_000_000_000) {
    return `${(n / 1_000_000_000).toFixed(2)} GB`;
  }
  if (n >= 1_000_000) {
    return `${(n / 1_000_000).toFixed(1)} MB`;
  }
  if (n >= 1_000) {
    return `${(n / 1_000).toFixed(1)} KB`;
  }
  return `${n} B`;
}

const banner = (bg: string, fg: string) => ({
  background: bg,
  color: fg,
  padding: "0.5rem 0.75rem",
  borderRadius: 6,
  fontSize: "0.875rem",
  margin: "0.5rem 0",
});

export default function MetricsDashboard({ metrics }: { metrics: WorkspaceMetrics }) {
  const { backend, replica, agents, retention } = metrics;

  return (
    <div data-testid="metrics-dashboard">
      <h2 style={ui.sectionHeading}>Backend capacity</h2>
      {backend === null ? (
        <p style={ui.muted}>
          Local-only workspace — nothing syncs to a shared backend, so there is no capacity to
          watch. {fmtBytes(replica.total_bytes)} on this machine.
        </p>
      ) : (
        <div style={ui.card}>
          <div
            style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}
          >
            <span>
              <strong>{backend.capacity.tier.toUpperCase()}</strong> tier ·{" "}
              {fmtBytes(backend.capacity.db_used_bytes)} of{" "}
              {fmtBytes(backend.capacity.db_limit_bytes)}
            </span>
            <span data-testid="capacity-pct">{(backend.capacity.db_pct * 100).toFixed(1)}%</span>
          </div>
          <div
            style={{
              height: 10,
              borderRadius: 999,
              background: ui.palette.surface,
              border: `1px solid ${ui.palette.border}`,
              overflow: "hidden",
              margin: "0.5rem 0",
            }}
          >
            <div
              data-testid="capacity-bar"
              style={{
                width: `${Math.min(100, backend.capacity.db_pct * 100)}%`,
                height: "100%",
                background: backend.capacity.headroom_warning
                  ? ui.palette.danger
                  : ui.palette.accent,
              }}
            />
          </div>
          {backend.capacity.headroom_warning && (
            <p
              style={banner(ui.palette.warnBg, ui.palette.warnText)}
              data-testid="headroom-warning"
            >
              ⚠ The {backend.capacity.tier} tier is about to bite (
              {(backend.capacity.db_pct * 100).toFixed(0)}% of the DB ceiling). Plan the upgrade to
              Pro.
            </p>
          )}
          {backend.capacity.idle_pause_risk && (
            <p
              style={banner(ui.palette.warnBg, ui.palette.warnText)}
              data-testid="idle-pause-warning"
            >
              ⚠ No recent activity — the Free tier pauses an idle project after 7 days.
            </p>
          )}
          {metrics.billing_url !== null && (
            <p style={{ marginBottom: 0 }}>
              <a href={metrics.billing_url} target="_blank" rel="noreferrer">
                View billing in Supabase ↗
              </a>{" "}
              <span style={ui.muted}>(the dollar bill lives in the Supabase console)</span>
            </p>
          )}
        </div>
      )}

      <h2 style={ui.sectionHeading}>Replica size by project</h2>
      <table style={ui.table}>
        <thead>
          <tr>
            <th style={ui.th}>Project</th>
            <th style={ui.th}>Rows</th>
            <th style={ui.th}>Size</th>
          </tr>
        </thead>
        <tbody>
          {replica.by_project.map((p) => (
            <tr key={p.project_id}>
              <td style={ui.td}>{p.name}</td>
              <td style={ui.td}>{p.rows}</td>
              <td style={ui.td}>{fmtBytes(p.bytes)}</td>
            </tr>
          ))}
          <tr>
            <td style={{ ...ui.td, fontWeight: 600 }}>Total</td>
            <td style={ui.td} />
            <td style={{ ...ui.td, fontWeight: 600 }} data-testid="replica-total">
              {fmtBytes(replica.total_bytes)}
            </td>
          </tr>
        </tbody>
      </table>

      <h2 style={ui.sectionHeading}>Agent activity ({agents.window_days}d)</h2>
      {agents.by_actor.length === 0 ? (
        <p style={ui.muted}>No agent activity in the window.</p>
      ) : (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.th}>Actor</th>
              <th style={ui.th}>Role</th>
              <th style={ui.th}>Calls</th>
              <th style={ui.th}>Reads</th>
              <th style={ui.th}>Proposes</th>
              <th style={ui.th}>Denials</th>
              <th style={ui.th}>~Tokens</th>
            </tr>
          </thead>
          <tbody>
            {agents.by_actor.map((a) => (
              <tr key={a.actor_id}>
                <td style={ui.td}>{a.actor_id}</td>
                <td style={ui.td}>{a.role}</td>
                <td style={ui.td}>{a.mcp_calls}</td>
                <td style={ui.td}>{a.reads}</td>
                <td style={ui.td}>{a.proposes}</td>
                <td style={ui.td}>{a.denials}</td>
                <td style={ui.td}>{a.est_tokens.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p style={{ ...ui.muted, marginTop: 4 }}>
        ~Tokens is a payload-size proxy (≈ MCP I/O bytes ÷ 4), not the agent's model tokens.
      </p>

      <h2 style={ui.sectionHeading}>Retention</h2>
      <div style={ui.card}>
        <p style={{ margin: 0 }}>
          <strong>MCP audit detail to summarize:</strong> {retention.audit_summarizable} rows
          {retention.audit_anchored ? "" : " (held — awaiting a Merkle anchor)"}.
        </p>
        <p style={{ ...ui.muted, marginBottom: 0 }}>
          Last run {fmtDateTime(retention.last_run)}. Audit detail older than 30 days summarizes
          once anchored; <code>sync_events</code> compacts below the safe watermark, backend-side.
        </p>
      </div>
    </div>
  );
}
