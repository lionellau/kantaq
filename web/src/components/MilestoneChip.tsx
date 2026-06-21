/**
 * E14 (MOD-20) — the per-row milestone count, next to the Sync/Proposal badges.
 * Neutral (the gray chip) like the relation badges: it is a navigational hint,
 * not an alert. Renders nothing when the ticket is in no milestone.
 */

import { chip } from "../lib/ui";

export default function MilestoneChip({ count }: { count: number }) {
  if (count === 0) {
    return null;
  }
  return (
    <output
      aria-label={`in ${count} milestone${count === 1 ? "" : "s"}`}
      data-count={count}
      style={chip}
    >
      {count} milestone{count === 1 ? "" : "s"}
    </output>
  );
}
