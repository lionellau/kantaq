/**
 * E20-T2 (MOD-12) — Settings → Identity: who this token belongs to, and the
 * capability grants derived from it.
 *
 * `/v1/me` gives the member (email, role, scopes); `/v1/grants` (self-scoped by
 * default) lists the short-lived, signed capability grants the member holds,
 * each with its resource, verbs, validity, and expiry — revocable here through
 * the same MOD-06 path the runtime uses. The agent connection snippet lives on
 * its own page (My Agent) and is linked from here.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { Grant, Me } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

/** Grant timestamps are unix seconds (they sign byte-identically); render them. */
function fmtUnix(seconds: number): string {
  return fmtDateTime(new Date(seconds * 1000).toISOString());
}

export default function Identity() {
  const { connected } = useSession();
  const [me, setMe] = useState<Me | null>(null);
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const meResult = await api.GET("/v1/me");
    if (meResult.error !== undefined) {
      setError("could not load your identity");
      return;
    }
    const grantsResult = await api.GET("/v1/grants");
    if (grantsResult.error !== undefined) {
      setError("could not load your grants");
      return;
    }
    setError(null);
    setMe(meResult.data);
    setGrants(grantsResult.data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function revoke(grant: Grant) {
    const { error: apiError } = await api.POST("/v1/grants/{grant_id}/revoke", {
      params: { path: { grant_id: grant.id } },
    });
    if (apiError !== undefined) {
      setError("could not revoke the grant");
      return;
    }
    setError(null);
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Identity</h1>
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
      <h1>Identity</h1>

      {error !== null && <p style={ui.errorText}>{error}</p>}

      {me !== null && (
        <div style={ui.card}>
          <p style={{ marginTop: 0, fontSize: "1.1rem", fontWeight: 600 }}>{me.email}</p>
          <p style={{ margin: "0 0 0.5rem" }}>
            <span style={ui.chip}>{me.role}</span>
          </p>
          <p style={ui.muted}>
            Member id <code>{me.member_id}</code>
          </p>
          <p style={ui.muted}>
            Token scopes:{" "}
            {me.scopes.length === 0 ? (
              <em>none — your role decides what you can do</em>
            ) : (
              me.scopes.map((scope) => (
                <span key={scope} style={{ ...ui.chip, marginRight: 4 }}>
                  {scope}
                </span>
              ))
            )}
          </p>
          <p style={{ marginBottom: 0 }}>
            <Link to="/settings/my-agent">Connect your coding agent →</Link>
          </p>
        </div>
      )}

      <h2 style={ui.sectionHeading}>Capability grants</h2>
      <p style={ui.muted}>
        Short-lived, signed permissions derived from your role (default 1 hour, 24 hour ceiling).
      </p>
      {grants !== null &&
        (grants.length === 0 ? (
          <p style={ui.muted}>No grants issued.</p>
        ) : (
          <table style={ui.table}>
            <thead>
              <tr>
                <th style={ui.th}>Resource</th>
                <th style={ui.th}>Verbs</th>
                <th style={ui.th}>Status</th>
                <th style={ui.th}>Expires</th>
                <th style={ui.th}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {grants.map((grant) => (
                <tr key={grant.id}>
                  <td style={ui.td}>
                    <code>{grant.resource}</code>
                  </td>
                  <td style={ui.td}>{grant.verbs.join(", ")}</td>
                  <td style={ui.td}>
                    <span style={ui.chip}>{grant.valid ? "valid" : grant.reason}</span>
                  </td>
                  <td style={{ ...ui.td, whiteSpace: "nowrap" }}>{fmtUnix(grant.expires_at)}</td>
                  <td style={ui.td}>
                    <button
                      type="button"
                      style={ui.dangerButton}
                      onClick={() => void revoke(grant)}
                      disabled={grant.revoked_at !== null}
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}
