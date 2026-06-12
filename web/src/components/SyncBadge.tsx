/**
 * E18-T3 — the sync-state badge primitive (MOD-10).
 *
 * Maps an entity's sync state to a small labelled chip:
 * - `draft`     — local only, not yet pushed (pending in the event log)
 * - `proposed`  — an agent proposal awaiting human approval (MOD-12 Inbox)
 * - `committed` — accepted by the backend and visible to the team
 *
 * A primitive on purpose: the backlog rows (E19) and the Inbox (E20) place it;
 * nothing here fetches. Colors are inline (the shell has no CSS framework) and
 * the state is also exposed via `data-state` + an accessible label.
 */

export type SyncState = "draft" | "proposed" | "committed";

const STYLES: Record<SyncState, { label: string; background: string; color: string }> = {
  draft: { label: "Draft", background: "#e8e8e8", color: "#444444" },
  proposed: { label: "Proposed", background: "#fff3cd", color: "#7a5c00" },
  committed: { label: "Committed", background: "#d9f2e3", color: "#1b6e3c" },
};

export default function SyncBadge({ state }: { state: SyncState }) {
  const style = STYLES[state];
  return (
    // <output> carries the implicit "status" ARIA role.
    <output
      aria-label={`sync state: ${style.label.toLowerCase()}`}
      data-state={state}
      style={{
        display: "inline-block",
        padding: "0.1rem 0.5rem",
        borderRadius: "999px",
        fontSize: "0.75rem",
        fontWeight: 600,
        background: style.background,
        color: style.color,
      }}
    >
      {style.label}
    </output>
  );
}
