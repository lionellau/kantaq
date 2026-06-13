/**
 * A freshly-minted bearer token, shown exactly once (NFR-E06-1). Used by the
 * Agents page when an admin rotates an agent's token: the plaintext exists only
 * in the rotate response, so it is surfaced once with copy + dismiss and then
 * gone (only the Argon2id hash is stored). Framework-free, on the shared `ui`
 * vocabulary (RISK-08).
 */

import { useState } from "react";
import * as ui from "../lib/ui";

export default function MintedToken({
  label,
  token,
  onDismiss,
}: {
  label: string;
  token: string;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
    } catch {
      // clipboard unavailable (insecure context): the token is selectable text
    }
  }
  return (
    <div role="alert" style={{ ...ui.card, borderColor: ui.palette.warnText }}>
      <p style={{ marginTop: 0 }}>{label} — shown once, store it now:</p>
      <code data-testid="minted-token" style={{ wordBreak: "break-all" }}>
        {token}
      </code>
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button type="button" style={ui.button} onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" style={ui.button} onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </div>
  );
}
