/**
 * E19 (MOD-11) — the per-row pending-proposal count, next to the SyncBadge.
 * Amber like the "proposed" sync state (the shared `warning` status token): it
 * means an agent is waiting on a human. Renders nothing when the count is zero.
 */

import { statusChip } from "../lib/ui";

export default function ProposalChip({ count }: { count: number }) {
  if (count === 0) {
    return null;
  }
  return (
    <output
      aria-label={`${count} pending proposal${count === 1 ? "" : "s"}`}
      data-count={count}
      style={statusChip("warning")}
    >
      {count} proposal{count === 1 ? "" : "s"}
    </output>
  );
}
