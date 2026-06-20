/**
 * The propose-first scopes a scoped Agent member carries (docs/mcp.md, D-03):
 * read tickets, store proposals — never a direct write. The single source of
 * truth for both the Members invite-Agent form and the My Agent snippet's
 * default scoped token (E20-T7), so the safe credential can never drift apart.
 */
export const AGENT_SCOPES = ["tickets.read", "proposals.write"] as const;
