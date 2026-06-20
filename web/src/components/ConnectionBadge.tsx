/**
 * E20-T7 (MOD-12 / MOD-08) — the honest agent-connection badge.
 *
 * The badge must never show an optimistic "connected" (a green that lies is
 * worse than no badge). It derives from two honest, loopback-only signals only:
 *   - `gatewayLive` — the gateway process is up (the `mcp.json` pid liveness
 *     check, FR-E21-3); when explicitly `false`, the agent cannot be attached.
 *   - `lastCallAt`  — the timestamp of the most-recent *successful* `source="mcp"`
 *     audit call (the caller filters out denied `tool.deny` rows: a stream of
 *     refused calls is activity but not health, and must never read as green).
 *     Green ("active") only inside the recent window; past it the badge says
 *     "idle", and with no successful call it says so.
 * There is no live-session push here: the gateway's in-process registry is not
 * reachable from the runtime (D-25), so the audit trail is the honest source.
 */

import { fmtRelative, isRecent } from "../lib/format";

const NEUTRAL = { background: "#e8e8e8", color: "#444444" };
const GREEN = { background: "#d9f2e3", color: "#1b6e3c" };

export default function ConnectionBadge({
  gatewayLive,
  lastCallAt,
}: {
  // `undefined` = liveness unknown (e.g. the workspace-wide Agents page, where
  // a per-machine gateway signal does not apply); `false` = known offline.
  gatewayLive?: boolean;
  lastCallAt: string | null;
}) {
  let label: string;
  let tone = NEUTRAL;

  if (gatewayLive === false) {
    label = "Gateway offline";
  } else if (lastCallAt !== null && isRecent(lastCallAt)) {
    label = `Active · last call ${fmtRelative(lastCallAt)}`;
    tone = GREEN;
  } else if (lastCallAt !== null) {
    label = `Idle · last call ${fmtRelative(lastCallAt)}`;
  } else {
    label = gatewayLive ? "Gateway up · no agent calls yet" : "No agent calls yet";
  }

  return (
    <output
      aria-label={`agent connection: ${label}`}
      data-tone={tone === GREEN ? "active" : "neutral"}
      style={{
        display: "inline-block",
        padding: "0.1rem 0.5rem",
        borderRadius: "999px",
        fontSize: "0.75rem",
        fontWeight: 600,
        ...tone,
      }}
    >
      {label}
    </output>
  );
}
