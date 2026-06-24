/**
 * E18-T3 — the sync-state badge primitive (MOD-10).
 *
 * Maps an entity's sync state to a small labelled chip:
 * - `draft`     — local only, not yet pushed (pending in the event log)
 * - `proposed`  — an agent proposal awaiting human approval (MOD-12 Inbox)
 * - `committed` — accepted by the backend and visible to the team
 *
 * A primitive on purpose: the backlog rows (E19) and the Inbox (E20) place it;
 * nothing here fetches. The tone comes from the shared semantic `status` tokens
 * (one success/warning/neutral set that re-themes in dark mode) via `statusChip`,
 * so no color is hardcoded; the state is also exposed via `data-state` + a label.
 */

import { type StatusKind, statusChip } from "../lib/ui";

export type SyncState = "draft" | "proposed" | "committed";

const STATE: Record<SyncState, { label: string; kind: StatusKind }> = {
  draft: { label: "Draft", kind: "neutral" },
  proposed: { label: "Proposed", kind: "warning" },
  committed: { label: "Committed", kind: "success" },
};

export default function SyncBadge({ state }: { state: SyncState }) {
  const { label, kind } = STATE[state];
  return (
    // <output> carries the implicit "status" ARIA role.
    <output
      aria-label={`sync state: ${label.toLowerCase()}`}
      data-state={state}
      style={statusChip(kind)}
    >
      {label}
    </output>
  );
}
