/**
 * E21-T2 + E20-T7 (MOD-13 / MOD-06 / MOD-08, SEC) — Settings → My Agent.
 *
 * The runtime returns the snippet skeleton with a placeholder where the token
 * goes (`/v1/me/agent-snippet` never carries a secret — NFR-E06-1); this page
 * substitutes a token client-side, so the secret never round-trips.
 *
 * E20-T7 makes the **safe path the easy path**: the default embedded credential
 * is a freshly-minted **scoped Agent token** (a propose-only Agent member —
 * `tickets.read` + `proposals.write`, the D-03 ceiling), issued through the
 * shipped Members invite path, *not* the human's own full-reach token. The
 * embedded identity is labelled. The owner-token path stays available as an
 * explicit, labelled opt-in, so a member who already pasted the owner snippet
 * keeps working. The panel **auto-detects** the gateway coming up (a 2 s poll,
 * no manual Reload), and a live **connection badge** reflects the gateway's pid
 * liveness + the most-recent audited MCP call — honest, never optimistic.
 *
 * Defense in depth on the URL: even though the server asserts loopback, the page
 * re-checks and refuses to render a snippet that points anywhere but the
 * member's own machine (FR-E21-3).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { AgentSnippet } from "../../api/types";
import ConnectionBadge from "../../components/ConnectionBadge";
import { AGENT_SCOPES } from "../../lib/agent";
import { getToken, useSession } from "../../lib/session";
import * as ui from "../../lib/ui";
import { usePolling } from "../../lib/usePolling";

const LOOPBACK_HOSTNAMES = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

export function isLoopback(url: string): boolean {
  try {
    return LOOPBACK_HOSTNAMES.has(new URL(url).hostname);
  } catch {
    return false;
  }
}

interface ScopedAgent {
  email: string;
  token: string;
}

export default function MyAgent() {
  const { connected } = useSession();
  const [snippet, setSnippet] = useState<AgentSnippet | null>(null);
  const [lastCallAt, setLastCallAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const [snipRes, callsRes] = await Promise.all([
      api.GET("/v1/me/agent-snippet"),
      api.GET("/v1/audit/range", { params: { query: { source: "mcp", limit: 25 } } }),
    ]);
    if (snipRes.error !== undefined) {
      setError("could not load the agent snippet");
      return;
    }
    setError(null);
    setSnippet(snipRes.data);
    // The badge greens on the last *successful* agent call only. A denied call
    // (a tool.deny row, which carries a `reason`) is real activity but not
    // health, so it must never read as "active" — the Inbox/Agents denied
    // surfaces are where denials belong.
    const lastSuccess = callsRes.data?.find((call) => call.reason === null);
    setLastCallAt(lastSuccess?.created_at ?? null);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  // Auto-detect the gateway: poll so the panel flips the moment `kantaq mcp dev`
  // comes up (and the connection badge stays current) — no manual Reload.
  usePolling(refresh, 2000, connected);

  if (!connected) {
    return (
      <section>
        <h1>My Agent</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <p style={{ margin: 0 }}>
        <Link to="/settings" style={ui.muted}>
          ← Settings
        </Link>
      </p>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <h1>My Agent</h1>
        <ConnectionBadge gatewayLive={snippet?.gateway_live ?? undefined} lastCallAt={lastCallAt} />
      </div>
      <p style={ui.muted}>
        Connect your coding agent to <strong>your own</strong> loopback MCP gateway. It reads
        tickets and proposes changes; nothing applies until a human approves it in the Inbox.
      </p>
      {error !== null && <p style={ui.errorText}>{error}</p>}
      {snippet !== null && <SnippetPanel snippet={snippet} />}
    </section>
  );
}

/** A unique key per (client, transport) — two transports share one client name. */
const snippetKey = (c: { client: string; transport: string }) => `${c.client}:${c.transport}`;

function SnippetPanel({ snippet }: { snippet: AgentSnippet }) {
  const [selectedKey, setSelectedKey] = useState<string>(
    snippet.clients[0] !== undefined ? snippetKey(snippet.clients[0]) : "",
  );
  // The default identity is a scoped Agent token (minted on demand); the owner
  // token is an explicit, labelled opt-in (it keeps existing setups working).
  const [agent, setAgent] = useState<ScopedAgent | null>(null);
  const [useOwnerToken, setUseOwnerToken] = useState(false);
  const [agentEmail, setAgentEmail] = useState("my-coding-agent@agents.local");
  const [genError, setGenError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function createScopedAgent() {
    setBusy(true);
    setGenError(null);
    const { data, error, response } = await api.POST("/v1/members/invite", {
      body: { email: agentEmail.trim(), role: "Agent", scopes: [...AGENT_SCOPES] },
    });
    setBusy(false);
    if (error !== undefined || data === undefined) {
      setGenError(
        response?.status === 403
          ? "your role may not invite an agent — ask an Owner or Maintainer"
          : "could not create the agent (is that email already taken?)",
      );
      return;
    }
    setAgent({ email: data.member.email, token: data.token });
  }

  // No transport offered at all → tell the member to start the gateway; we
  // auto-detect (poll) — no Reload. (stdio alone needs no live gateway, so this
  // only fires when there is genuinely nothing to connect with.)
  if (snippet.clients.length === 0) {
    return (
      <div style={ui.card}>
        <p style={{ marginTop: 0 }}>
          Start the MCP gateway on this machine — this panel detects it automatically:
        </p>
        <pre style={{ background: ui.palette.surface, padding: "0.5rem" }}>kantaq mcp dev</pre>
        <p style={ui.muted}>Waiting for your gateway… (no need to reload)</p>
      </div>
    );
  }

  // The HTTP variants need a loopback URL; stdio does not. When a URL is present
  // it must be loopback (an agent connects only to this machine).
  if (snippet.gateway_url !== null && !isLoopback(snippet.gateway_url)) {
    return (
      <p role="alert" style={ui.errorText}>
        Refusing to render: the gateway URL {snippet.gateway_url} is not loopback. Your agent must
        connect to your own 127.0.0.1 runtime only.
      </p>
    );
  }

  // Live HTTP gateway? If not, only the stdio configs are offered (they launch
  // `kantaq mcp stdio` themselves — no HTTP endpoint needed).
  const httpLive = snippet.gateway_live && snippet.gateway_url !== null;
  const selected = snippet.clients.find((c) => snippetKey(c) === selectedKey) ?? snippet.clients[0];
  // The token to embed: the scoped Agent token by default (null until minted, so
  // the snippet shows the placeholder, never the owner token); the owner token
  // only on explicit opt-in.
  const token = useOwnerToken ? getToken() : (agent?.token ?? null);
  const sub = (text: string) =>
    token !== null ? text.replaceAll(snippet.token_placeholder, token) : text;
  const rendered = sub(selected.text);
  const setup = selected.setup !== null ? sub(selected.setup) : null;

  const preStyle = {
    background: ui.palette.surface,
    padding: "0.75rem",
    overflowX: "auto",
    fontSize: "0.8rem",
  } as const;

  return (
    <div style={ui.card}>
      <AgentIdentity
        agent={agent}
        useOwnerToken={useOwnerToken}
        agentEmail={agentEmail}
        busy={busy}
        genError={genError}
        onEmailChange={setAgentEmail}
        onCreate={() => void createScopedAgent()}
        onToggleOwner={(v) => setUseOwnerToken(v)}
      />

      {!httpLive && (
        <p style={{ ...ui.muted, marginBottom: 0 }}>
          The HTTP gateway isn't running, so only the <strong>stdio</strong> configs are shown —
          they launch <code>kantaq mcp stdio</code> themselves and need no gateway. Run{" "}
          <code>kantaq mcp dev</code> for the HTTP option.
        </p>
      )}
      <div
        role="tablist"
        aria-label="Coding agent"
        style={{ display: "flex", gap: "0.5rem", marginTop: "0.75rem", flexWrap: "wrap" }}
      >
        {snippet.clients.map((client) => (
          <button
            key={snippetKey(client)}
            type="button"
            role="tab"
            aria-selected={snippetKey(client) === snippetKey(selected)}
            style={snippetKey(client) === snippetKey(selected) ? ui.primaryButton : ui.button}
            onClick={() => setSelectedKey(snippetKey(client))}
          >
            {client.label}
          </button>
        ))}
      </div>
      {setup !== null && (
        <>
          <p style={{ marginBottom: 0 }}>First, export your token in the shell that runs Codex:</p>
          <pre data-testid="agent-setup" style={preStyle}>
            {setup}
          </pre>
        </>
      )}
      <p style={{ marginBottom: 0 }}>
        {setup !== null ? "Then add this to " : "Save this as "}
        <code>{selected.save_as}</code> ({selected.label}):
      </p>
      <pre data-testid="agent-snippet" style={preStyle}>
        {rendered}
      </pre>
      <p style={ui.muted}>
        {selected.transport === "stdio" ? (
          <>Transport: stdio (your agent launches the gateway on this machine). </>
        ) : (
          <>
            Gateway: <code>{snippet.gateway_url}</code> (your machine only).{" "}
          </>
        )}
        {token === null
          ? "Create a scoped agent above to fill in the token."
          : selected.transport === "stdio"
            ? "Your token rides the KANTAQ_MCP_TOKEN env var in the config; treat it like a credential."
            : setup !== null
              ? "The config file carries no token — your token rides the KANTAQ_AGENT_TOKEN env var above; treat it like a credential."
              : "Treat the file like a credential. Rotate it from Members if it leaks."}
      </p>
      <CopySnippet
        text={setup !== null ? `${setup}\n\n${rendered}` : rendered}
        disabled={token === null}
      />
    </div>
  );
}

function AgentIdentity({
  agent,
  useOwnerToken,
  agentEmail,
  busy,
  genError,
  onEmailChange,
  onCreate,
  onToggleOwner,
}: {
  agent: ScopedAgent | null;
  useOwnerToken: boolean;
  agentEmail: string;
  busy: boolean;
  genError: string | null;
  onEmailChange: (value: string) => void;
  onCreate: () => void;
  onToggleOwner: (value: boolean) => void;
}) {
  if (useOwnerToken) {
    return (
      <div style={{ ...ui.card, background: ui.palette.warnBg, borderColor: ui.palette.warnText }}>
        <p style={{ margin: 0 }}>
          <strong>Using your own member token (full access).</strong> Your agent will act with your
          full reach. Prefer a scoped agent —{" "}
          <button type="button" style={ui.linkButton} onClick={() => onToggleOwner(false)}>
            switch back to a scoped agent token
          </button>
          .
        </p>
      </div>
    );
  }

  if (agent !== null) {
    return (
      <div style={ui.card}>
        <p style={{ margin: 0 }}>
          Embedded identity: <strong>{agent.email}</strong>{" "}
          <span style={ui.chip}>Agent · propose-only</span>
        </p>
        <p style={{ ...ui.muted, margin: "0.35rem 0 0" }}>
          This token is shown once, here, embedded in the snippet below. Manage agents from{" "}
          <Link to="/settings/members">Members</Link>; rotate it there if it leaks.
        </p>
      </div>
    );
  }

  return (
    <div style={ui.card}>
      <p style={{ marginTop: 0 }}>
        Create a <strong>scoped agent token</strong> — a propose-only identity (
        {AGENT_SCOPES.join(", ")}). Your agent never runs with your full reach, and its actions are
        attributed to it, not you.
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
        <label style={ui.label}>
          Agent name (email)
          <input
            style={{ ...ui.input, minWidth: "16rem" }}
            aria-label="agent email"
            value={agentEmail}
            onChange={(event) => onEmailChange(event.target.value)}
          />
        </label>
        <button type="button" style={ui.primaryButton} disabled={busy} onClick={onCreate}>
          Create scoped agent token
        </button>
      </div>
      {genError !== null && <p style={ui.errorText}>{genError}</p>}
      <p style={{ ...ui.muted, margin: "0.5rem 0 0" }}>
        Or invite an agent from <Link to="/settings/members">Members</Link>, or{" "}
        <button type="button" style={ui.linkButton} onClick={() => onToggleOwner(true)}>
          use your own member token
        </button>{" "}
        (full access).
      </p>
    </div>
  );
}

function CopySnippet({ text, disabled }: { text: string; disabled: boolean }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // clipboard unavailable: the snippet is selectable text
    }
  }

  return (
    <button type="button" style={ui.primaryButton} disabled={disabled} onClick={() => void copy()}>
      {copied ? "Copied" : "Copy snippet"}
    </button>
  );
}
