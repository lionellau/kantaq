/**
 * E28-T1 (MOD-25) — Settings → Telemetry: the opt-in toggle and the local
 * inspection view (FR-E28-3, D-10).
 *
 * The page shows exactly what the machine has collected — the computed
 * outcome metrics plus every raw event row — because the privacy promise is
 * transparency: telemetry is off by default, local-only (no collector), and
 * never contains ticket or memory content. Sharing anything is a manual act
 * by the user.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { TelemetryView } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

function fmtRate(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

function fmtSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (value < 90) {
    return `${Math.round(value)}s`;
  }
  return value < 5400 ? `${Math.round(value / 60)}m` : `${(value / 3600).toFixed(1)}h`;
}

export default function Telemetry() {
  const { connected } = useSession();
  const [view, setView] = useState<TelemetryView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/telemetry");
    if (apiError !== undefined) {
      setError("could not load telemetry");
      return;
    }
    setError(null);
    setView(data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function toggle() {
    if (view === null) {
      return;
    }
    setBusy(true);
    const { data, error: apiError, response } = await api.PUT("/v1/telemetry", {
      body: { enabled: !view.enabled },
    });
    setBusy(false);
    if (apiError !== undefined || data === undefined) {
      setError(
        response?.status === 403
          ? "only an Owner or Maintainer may change the telemetry setting"
          : "could not update telemetry",
      );
      return;
    }
    setError(null);
    setView(data);
  }

  if (!connected) {
    return (
      <section>
        <h1>Telemetry</h1>
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
      <h1>Telemetry</h1>
      <p style={ui.muted}>
        Off by default. When on, kantaq records anonymous usage counts and timings <em>locally</em>{" "}
        — there is no remote collector, and ticket or memory content is never recorded. Everything
        collected is listed below; sharing it is always a manual act.
      </p>

      {error !== null && <p style={ui.errorText}>{error}</p>}

      {view !== null && (
        <>
          <div style={{ ...ui.card, display: "flex", alignItems: "center", gap: 12 }}>
            <output data-testid="telemetry-state">
              Telemetry is <strong>{view.enabled ? "on" : "off"}</strong>
            </output>
            <button
              type="button"
              style={view.enabled ? ui.button : ui.primaryButton}
              onClick={() => void toggle()}
              disabled={busy}
            >
              {view.enabled ? "Turn off" : "Turn on"}
            </button>
          </div>

          <h2 style={ui.sectionHeading}>Outcome metrics</h2>
          <table style={ui.table}>
            <tbody>
              <tr>
                <td style={ui.td}>Proposal acceptance rate</td>
                <td style={ui.td}>{fmtRate(view.metrics.proposal_acceptance_rate)}</td>
              </tr>
              <tr>
                <td style={ui.td}>Median time to approve</td>
                <td style={ui.td}>{fmtSeconds(view.metrics.median_seconds_to_approve)}</td>
              </tr>
              <tr>
                <td style={ui.td}>Install → first proposal</td>
                <td style={ui.td}>{fmtSeconds(view.metrics.install_to_first_proposal_seconds)}</td>
              </tr>
              <tr>
                <td style={ui.td}>MCP sessions (members with repeats)</td>
                <td style={ui.td}>
                  {view.metrics.mcp_sessions_total} ({view.metrics.repeat_session_members})
                </td>
              </tr>
              <tr>
                <td style={ui.td}>Activity-feed views</td>
                <td style={ui.td}>{view.metrics.activity_views_total}</td>
              </tr>
              <tr>
                <td style={ui.td}>Active this week</td>
                <td style={ui.td}>{view.metrics.weekly_active ? "yes" : "no"}</td>
              </tr>
            </tbody>
          </table>

          <h2 style={ui.sectionHeading}>Captured events ({view.metrics.events_total})</h2>
          {view.events.length === 0 ? (
            <p style={ui.muted}>Nothing recorded.</p>
          ) : (
            <table style={ui.table}>
              <thead>
                <tr>
                  <th style={ui.th}>When</th>
                  <th style={ui.th}>Event</th>
                  <th style={ui.th}>Props</th>
                </tr>
              </thead>
              <tbody>
                {view.events.map((event) => (
                  <tr key={event.id}>
                    <td style={{ ...ui.td, whiteSpace: "nowrap" }}>
                      {fmtDateTime(event.created_at)}
                    </td>
                    <td style={ui.td}>
                      <span style={ui.chip}>{event.name}</span>
                    </td>
                    <td style={ui.td}>
                      <code style={{ fontSize: "0.8rem" }}>{JSON.stringify(event.props)}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
