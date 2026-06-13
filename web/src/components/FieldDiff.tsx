/**
 * E20-T3 (MOD-12) — the field-level before→after diff for a proposal.
 *
 * A proposal's `diff.changes` is a handful of ticket fields (status, priority,
 * assignee, labels, title, …). This renders each as the ticket's *current*
 * value struck through, an arrow, and the proposed value — so a human approving
 * from the Inbox sees exactly what flips, against the live ticket, not just the
 * proposed side.
 *
 * Golden rule (recorded in docs/stack.md): the proposed values are **untrusted**
 * agent text (PRD §15), and this is a *structured* per-field diff, not a
 * text/code-block diff. The diff-viewer candidates either sit under the 5k-star
 * bar (react-diff-view, react-diff-viewer-continued) or emit HTML strings that
 * force `dangerouslySetInnerHTML` (jsondiffpatch) — banned here for the same
 * reason the body uses react-markdown. jsdiff clears the bar but only does
 * char/word/line diffing of one string, which buys little on mostly-short
 * fields. So this renders plain text from scratch (RISK-08), values JSON-shaped,
 * never markup.
 */

import * as ui from "../lib/ui";

/** A proposed/current value to a plain display string — never markup. */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? "—" : value.map((v) => String(v)).join(", ");
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

export default function FieldDiff({
  field,
  before,
  after,
}: {
  field: string;
  before: unknown;
  after: unknown;
}) {
  const beforeText = displayValue(before);
  const afterText = displayValue(after);
  const unchanged = beforeText === afterText;
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <span style={{ ...ui.label, textTransform: "none" }}>{field}</span>
      <div
        style={{
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "baseline",
          fontSize: "0.875rem",
        }}
      >
        <span
          style={{
            color: ui.palette.muted,
            textDecoration: unchanged ? "none" : "line-through",
            fontFamily: "monospace",
          }}
        >
          {beforeText}
        </span>
        <span aria-hidden style={{ color: ui.palette.muted }}>
          →
        </span>
        <span style={{ color: ui.palette.text, fontFamily: "monospace", fontWeight: 600 }}>
          {afterText}
        </span>
      </div>
    </div>
  );
}
