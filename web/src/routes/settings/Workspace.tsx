/**
 * E20-T2 (MOD-12) — Settings → Workspace: the shared workspace and its
 * workspace-level settings (Members, Telemetry). The workspace name and id
 * come from `/v1/me`; the management surfaces keep living on their own pages
 * (MOD-13 Members, MOD-25 Telemetry) and are linked here under their parent.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { Me } from "../../api/types";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

export default function Workspace() {
  const { connected } = useSession();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/me");
    if (apiError !== undefined) {
      setError("could not load the workspace");
      return;
    }
    setError(null);
    setMe(data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!connected) {
    return (
      <section>
        <h1>Workspace</h1>
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
      <h1>Workspace</h1>

      {error !== null && <p style={ui.errorText}>{error}</p>}

      {me !== null && (
        <div style={ui.card}>
          <p style={{ marginTop: 0, fontSize: "1.1rem", fontWeight: 600 }}>{me.workspace_name}</p>
          <p style={ui.muted}>
            Workspace id <code>{me.workspace_id}</code>
          </p>
        </div>
      )}

      <h2 style={ui.sectionHeading}>Workspace settings</h2>
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 8 }}>
        <li>
          <Link to="/settings/members">Members</Link>
          <span style={ui.muted}> — invite teammates, revoke or rotate tokens</span>
        </li>
        <li>
          <Link to="/settings/telemetry">Telemetry</Link>
          <span style={ui.muted}> — opt-in usage metrics; inspect exactly what is collected</span>
        </li>
      </ul>
    </section>
  );
}
