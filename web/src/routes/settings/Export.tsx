/**
 * E20-T2 (MOD-12) — Settings → Export: the stub for workspace export.
 *
 * The full portable export (the whole workspace as JSON you own, with its audit
 * trail, re-importable elsewhere) is MOD-23 and lands in a later release. This
 * page names the promise and the format so the Settings tree is complete; the
 * action stays disabled until the export pipeline exists rather than wired to
 * something that half-works.
 */

import { Link } from "react-router-dom";
import * as ui from "../../lib/ui";

export default function Export() {
  return (
    <section>
      <p style={{ margin: 0 }}>
        <Link to="/settings" style={ui.muted}>
          ← Settings
        </Link>
      </p>
      <h1>Export</h1>
      <p style={ui.muted}>
        Take your workspace with you. Export will write the whole workspace — tickets, comments,
        memory, and the audit trail — as portable JSON you own and can re-import elsewhere.
      </p>
      <div style={ui.card}>
        <p style={{ marginTop: 0 }}>
          <strong>Not available yet.</strong> Portable export lands with the export module (MOD-23).
          kantaq is local-first, so your data already lives on this machine at{" "}
          <code>data/local.sqlite</code> in the meantime.
        </p>
        <button type="button" style={ui.button} disabled aria-disabled="true">
          Export workspace
        </button>
      </div>
    </section>
  );
}
