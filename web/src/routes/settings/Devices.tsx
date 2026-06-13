/**
 * E20-T2 (MOD-12) — Settings → Devices: the workspace's registered signing
 * identities (the root-of-trust map).
 *
 * Each row is one runtime's Ed25519 verify key — the private seed never leaves
 * that machine, so only the public key is ever shown. Any member may read the
 * list (public material); decommissioning a device is a credential-management
 * action (Maintainer+) that drops it from the trust map and revokes the grants
 * it issued. This runtime's own active device is marked and cannot be
 * decommissioned from here (it would strand grant issuance until a re-key).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { Device } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

function maskKey(publicKey: string): string {
  return publicKey.length <= 20 ? publicKey : `${publicKey.slice(0, 10)}…${publicKey.slice(-6)}`;
}

export default function Devices() {
  const { connected } = useSession();
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/devices");
    if (apiError !== undefined) {
      setError("could not load devices");
      return;
    }
    setError(null);
    setDevices(data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function decommission(device: Device) {
    const { error: apiError, response } = await api.POST("/v1/devices/{device_id}/revoke", {
      params: { path: { device_id: device.id } },
    });
    if (apiError !== undefined) {
      setError(
        response?.status === 403
          ? "only a Maintainer or Owner may decommission a device"
          : response?.status === 409
            ? "cannot decommission this runtime's own active device"
            : `could not decommission ${device.label || device.id}`,
      );
      return;
    }
    setError(null);
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Devices</h1>
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
      <h1>Devices</h1>
      <p style={ui.muted}>
        The signing identities registered to this workspace. Each is one runtime's public key — the
        private key never leaves that machine. Decommissioning a device removes it as a trusted
        signer and revokes the grants it issued.
      </p>

      {error !== null && <p style={ui.errorText}>{error}</p>}

      {devices !== null &&
        (devices.length === 0 ? (
          <p style={ui.muted}>No devices registered.</p>
        ) : (
          <table style={ui.table}>
            <thead>
              <tr>
                <th style={ui.th}>Device</th>
                <th style={ui.th}>Public key</th>
                <th style={ui.th}>Member</th>
                <th style={ui.th}>Registered</th>
                <th style={ui.th}>Status</th>
                <th style={ui.th}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => (
                <tr key={device.id}>
                  <td style={ui.td}>
                    {device.label || "device"}
                    {device.is_current && (
                      <span style={{ ...ui.chip, marginLeft: 6 }}>this runtime</span>
                    )}
                  </td>
                  <td style={ui.td}>
                    <code title={device.public_key}>{maskKey(device.public_key)}</code>
                  </td>
                  <td style={ui.td}>{device.member_email ?? "—"}</td>
                  <td style={{ ...ui.td, whiteSpace: "nowrap" }}>
                    {fmtDateTime(device.created_at)}
                  </td>
                  <td style={ui.td}>
                    <span style={ui.chip}>{device.active ? "active" : "decommissioned"}</span>
                  </td>
                  <td style={ui.td}>
                    <button
                      type="button"
                      style={ui.dangerButton}
                      onClick={() => void decommission(device)}
                      disabled={!device.active || device.is_current}
                      title={
                        device.is_current
                          ? "this runtime's own device cannot be decommissioned here"
                          : undefined
                      }
                    >
                      Decommission
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
