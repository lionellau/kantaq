/**
 * Settings → Export: download the whole workspace as a portable bundle.
 *
 * The export pipeline (MOD-23, E23) shipped in v0.2 — ``POST /v1/export``
 * returns the deterministic gzip tarball ``kantaq_runtime.export`` builds
 * (JSON event logs + the audit trail + content-addressed blobs), re-importable
 * elsewhere. DEBT-34 wires this button to that endpoint; it was previously a
 * dead control under copy that fully described the capability — a broken
 * promise for a local-first, data-sovereignty product.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { authFetch } from "../../api/client";
import * as ui from "../../lib/ui";

const EXPORT_FILENAME = "kantaq-export.tar.gz";
const FALLBACK = "Your data still lives on this machine at data/local.sqlite in the meantime.";

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export default function Export() {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onExport(): Promise<void> {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const response = await authFetch("/v1/export", { method: "POST" });
      if (!response.ok) {
        setError(`Export failed (${response.status}). ${FALLBACK}`);
        return;
      }
      triggerDownload(await response.blob(), EXPORT_FILENAME);
      setNotice(`Downloaded ${EXPORT_FILENAME} — your whole workspace, re-importable elsewhere.`);
    } catch {
      setError(`Export failed — the runtime did not respond. ${FALLBACK}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <p style={{ margin: 0 }}>
        <Link to="/settings" style={ui.muted}>
          ← Settings
        </Link>
      </p>
      <h1>Export</h1>
      <p style={ui.muted}>
        Take your workspace with you. Export writes the whole workspace — tickets, comments, memory,
        and the audit trail — as a portable bundle (gzipped JSON event logs you own and can
        re-import elsewhere).
      </p>
      <div style={ui.card}>
        <p style={{ marginTop: 0 }}>
          Downloads <code>{EXPORT_FILENAME}</code>: a deterministic, signed bundle of the entire
          workspace. kantaq is local-first, so your data already lives on this machine at{" "}
          <code>data/local.sqlite</code> — this is the portable, re-importable copy.
        </p>
        <button
          type="button"
          style={busy ? ui.button : ui.primaryButton}
          onClick={onExport}
          disabled={busy}
          aria-disabled={busy}
        >
          {busy ? "Exporting…" : "Export workspace"}
        </button>
        {notice !== null && (
          <p style={{ ...ui.muted, marginBottom: 0 }}>
            <output>{notice}</output>
          </p>
        )}
        {error !== null && (
          <p role="alert" style={{ ...ui.errorText, marginBottom: 0 }}>
            {error}
          </p>
        )}
      </div>
    </section>
  );
}
