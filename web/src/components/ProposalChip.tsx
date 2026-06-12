/**
 * E19 (MOD-11) — the per-row pending-proposal count, next to the SyncBadge.
 * Amber like the "proposed" sync state: it means an agent is waiting on a
 * human. Renders nothing when the count is zero.
 */

import { palette } from "../lib/ui";

export default function ProposalChip({ count }: { count: number }) {
  if (count === 0) {
    return null;
  }
  return (
    <output
      aria-label={`${count} pending proposal${count === 1 ? "" : "s"}`}
      data-count={count}
      style={{
        display: "inline-block",
        padding: "0.1rem 0.5rem",
        borderRadius: "999px",
        fontSize: "0.75rem",
        fontWeight: 600,
        background: palette.warnBg,
        color: palette.warnText,
      }}
    >
      {count} proposal{count === 1 ? "" : "s"}
    </output>
  );
}
