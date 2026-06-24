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
import { statusChip } from "../lib/ui";

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
  let active = false;

  if (gatewayLive === false) {
    label = "Gateway offline";
  } else if (lastCallAt !== null && isRecent(lastCallAt)) {
    label = `Active · last call ${fmtRelative(lastCallAt)}`;
    active = true;
  } else if (lastCallAt !== null) {
    label = `Idle · last call ${fmtRelative(lastCallAt)}`;
  } else {
    label = gatewayLive ? "Gateway up · no agent calls yet" : "No agent calls yet";
  }

  return (
    <output
      aria-label={`agent connection: ${label}`}
      data-tone={active ? "active" : "neutral"}
      style={statusChip(active ? "success" : "neutral")}
    >
      {label}
    </output>
  );
}
