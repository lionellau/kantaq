/**
 * E20-T5 (MOD-12 / MOD-26 §B4) — one sync-conflict record, reviewable.
 *
 * Two concurrent writes set the same field to different values; last-writer-wins
 * already converged the field to one of them (ride-flagged, D-17), and this card
 * is the human's audit-and-correct surface. It renders the field path, the
 * losing write's actor and the revisions that collided, and the two candidate
 * values (`keep_a` = the committed head, `keep_b` = the loser) — then lets a
 * maintainer pick a side or type a new value. Picking calls `resolve_conflict`;
 * if the field moved since the record was minted the resolution does not apply
 * and the parent surfaces a re-decide notice (rebase_required).
 *
 * Candidate values are untrusted data — rendered as plain text via FieldDiff,
 * never markup (PRD §15).
 */

import { useState } from "react";
import type { Conflict } from "../api/types";
import * as ui from "../lib/ui";
import { displayValue } from "./FieldDiff";

export default function ConflictCard({
  conflict,
  busy,
  onResolve,
}: {
  conflict: Conflict;
  busy: boolean;
  onResolve: (choice: "keep-A" | "keep-B" | "new-value", newValue?: string) => void;
}) {
  const [newValue, setNewValue] = useState("");
  const [showNew, setShowNew] = useState(false);
  const keepA = conflict.candidate_values?.keep_a;
  const keepB = conflict.candidate_values?.keep_b;

  return (
    <li style={ui.card}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600 }}>
          {conflict.collection}/{conflict.entity_id} · <code>{conflict.field}</code>
        </span>
        <span style={ui.chip}>conflict</span>
      </div>
      <p style={{ ...ui.muted, margin: "0.35rem 0" }}>
        {conflict.actor}'s write lost the last-writer-wins tie. Revisions{" "}
        {conflict.contending_revisions.join(" vs ")} · base {conflict.base_rev} · head{" "}
        {conflict.head_rev}. Pick the value that should stand.
      </p>

      <div style={{ display: "grid", gap: 6, margin: "0.5rem 0" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
          <span style={{ ...ui.label, textTransform: "none" }}>A — current (kept)</span>
          <span style={{ fontFamily: "monospace", fontWeight: 600 }} data-testid="conflict-keep-a">
            {displayValue(keepA)}
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
          <span style={{ ...ui.label, textTransform: "none" }}>B — incoming</span>
          <span style={{ fontFamily: "monospace", fontWeight: 600 }} data-testid="conflict-keep-b">
            {displayValue(keepB)}
          </span>
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <button
          type="button"
          style={ui.primaryButton}
          disabled={busy}
          onClick={() => onResolve("keep-A")}
        >
          Keep A
        </button>
        <button type="button" style={ui.button} disabled={busy} onClick={() => onResolve("keep-B")}>
          Keep B
        </button>
        <button
          type="button"
          style={ui.button}
          disabled={busy}
          onClick={() => setShowNew((v) => !v)}
        >
          New value…
        </button>
      </div>

      {showNew && (
        <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
          <input
            style={ui.input}
            aria-label="new value"
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            placeholder="a new value to set"
          />
          <button
            type="button"
            style={ui.primaryButton}
            disabled={busy || newValue === ""}
            onClick={() => onResolve("new-value", newValue)}
          >
            Set
          </button>
        </div>
      )}
    </li>
  );
}
