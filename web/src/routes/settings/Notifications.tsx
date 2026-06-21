/**
 * E20-T9 (MOD-12 / PRD §16.10) — Settings → Notifications.
 *
 * Configure the outbound sink + the opt-in toggle (default off). The signal is
 * content-free: when on, kantaq POSTs `{action, ids, actor, deep-link}` — never
 * a ticket or memory body — to the sink on approve / reject / conflict, so an
 * async teammate stops refreshing the Inbox.
 *
 * The page never receives the configured URL back (the API returns the host
 * only — a Slack webhook path carries a secret), so a stored sink can be toggled
 * on/off without re-entering it; entering a new URL replaces it. Configuring is
 * a Maintainer+ action (the API returns 403 otherwise).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { NotificationConfig } from "../../api/types";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

export default function Notifications() {
  const { connected } = useSession();
  const [config, setConfig] = useState<NotificationConfig | null>(null);
  const [sinkType, setSinkType] = useState("webhook");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/notifications");
    if (apiError !== undefined || data === undefined) {
      setError("could not load notifications");
      return;
    }
    setError(null);
    setConfig(data);
    setSinkType(data.sink_type);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function save(enabled: boolean) {
    setBusy(true);
    setNotice(null);
    const {
      data,
      error: apiError,
      response,
    } = await api.PUT("/v1/notifications", {
      body: { enabled, sink_type: sinkType, webhook_url: webhookUrl.trim() || null },
    });
    setBusy(false);
    if (apiError !== undefined || data === undefined) {
      setError(
        response?.status === 403
          ? "only an Owner or Maintainer may change notifications"
          : response?.status === 422
            ? "enabling needs a valid http(s) sink URL with no embedded credentials"
            : "could not update notifications",
      );
      return;
    }
    setError(null);
    setConfig(data);
    setWebhookUrl(""); // never keep the secret URL in the field
    setNotice(enabled ? "Notifications on." : "Notifications off.");
  }

  if (!connected) {
    return (
      <section>
        <h1>Notifications</h1>
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
      <h1>Notifications</h1>
      <p style={ui.muted}>
        Off by default. When on, kantaq sends a <strong>content-free</strong> signal (the action +
        ids + a deep-link — never ticket or memory content) to your sink when a proposal is approved
        or rejected, or a sync conflict is minted, so an async teammate stops refreshing the Inbox.
      </p>

      {error !== null && <p style={ui.errorText}>{error}</p>}
      {notice !== null && <p data-testid="notifications-notice">{notice}</p>}

      {config !== null && (
        <div style={{ ...ui.card, display: "grid", gap: 12 }}>
          <output data-testid="notifications-state">
            Notifications are <strong>{config.enabled ? "on" : "off"}</strong>
            {config.configured && config.sink_host ? ` → ${config.sink_host}` : ""}
          </output>

          <label style={ui.label}>
            Sink type
            <select
              aria-label="sink type"
              style={ui.input}
              value={sinkType}
              onChange={(event) => setSinkType(event.target.value)}
            >
              <option value="webhook">Webhook (generic POST)</option>
              <option value="slack">Slack (incoming webhook)</option>
            </select>
          </label>

          <label style={ui.label}>
            Sink URL
            <input
              type="url"
              aria-label="sink url"
              style={ui.input}
              value={webhookUrl}
              onChange={(event) => setWebhookUrl(event.target.value)}
              placeholder={
                config.configured
                  ? `configured → ${config.sink_host} (enter a new URL to replace it)`
                  : "https://hooks.slack.com/services/…"
              }
            />
          </label>

          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              style={ui.primaryButton}
              onClick={() => void save(true)}
              disabled={busy}
            >
              {config.enabled ? "Save" : "Save + turn on"}
            </button>
            {config.enabled && (
              <button
                type="button"
                style={ui.button}
                onClick={() => void save(false)}
                disabled={busy}
              >
                Turn off
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
