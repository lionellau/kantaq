/**
 * E21-T2 (MOD-13, SEC) — Settings → My Agent: the loopback MCP snippet.
 *
 * The runtime returns the snippet skeleton with a placeholder where the token
 * goes (`/v1/me/agent-snippet` never carries a secret — NFR-E06-1); this page
 * substitutes the member's own session token client-side, so the secret never
 * makes a round trip. Defense in depth on the URL: even though the server
 * already asserts loopback, the page re-checks and refuses to render a
 * snippet that points anywhere but the member's own machine (FR-E21-3 — the
 * agent talks to *your* runtime, never someone else's).
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { AgentSnippet } from "../../api/types";
import { getToken, useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

const LOOPBACK_HOSTNAMES = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

export function isLoopback(url: string): boolean {
  try {
    return LOOPBACK_HOSTNAMES.has(new URL(url).hostname);
  } catch {
    return false;
  }
}

export default function MyAgent() {
  const { connected } = useSession();
  const [snippet, setSnippet] = useState<AgentSnippet | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/me/agent-snippet");
    if (apiError !== undefined) {
      setError("could not load the agent snippet");
      return;
    }
    setError(null);
    setSnippet(data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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
      <h1>My Agent</h1>
      <p style={ui.muted}>
        Connect your coding agent to <strong>your own</strong> loopback MCP gateway. It reads
        tickets and proposes changes; nothing applies until a human approves it in the Inbox.
      </p>
      {error !== null && <p style={ui.errorText}>{error}</p>}
      {snippet !== null && <SnippetPanel snippet={snippet} onReload={() => void refresh()} />}
    </section>
  );
}

function SnippetPanel({ snippet, onReload }: { snippet: AgentSnippet; onReload: () => void }) {
  if (!snippet.gateway_live || snippet.gateway_url === null || snippet.snippet === null) {
    return (
      <div style={ui.card}>
        <p style={{ marginTop: 0 }}>
          The MCP gateway is not running. Start it on this machine, then reload:
        </p>
        <pre style={{ background: ui.palette.surface, padding: "0.5rem" }}>kantaq mcp dev</pre>
        <button type="button" style={ui.button} onClick={onReload}>
          Reload
        </button>
      </div>
    );
  }

  if (!isLoopback(snippet.gateway_url)) {
    // Should be unreachable (the server asserts loopback too), but a snippet
    // pointing at another host is exactly the failure E21-T2 exists to stop.
    return (
      <p role="alert" style={ui.errorText}>
        Refusing to render: the gateway URL {snippet.gateway_url} is not loopback. Your agent must
        connect to your own 127.0.0.1 runtime only.
      </p>
    );
  }

  const token = getToken();
  const rendered = JSON.stringify(snippet.snippet, null, 2).replaceAll(
    snippet.token_placeholder,
    token ?? snippet.token_placeholder,
  );

  return (
    <div style={ui.card}>
      <p style={{ marginTop: 0 }}>
        Save this as <code>.mcp.json</code> in your project (Claude Code reads it on start):
      </p>
      <pre
        data-testid="agent-snippet"
        style={{
          background: ui.palette.surface,
          padding: "0.75rem",
          overflowX: "auto",
          fontSize: "0.8rem",
        }}
      >
        {rendered}
      </pre>
      <p style={ui.muted}>
        Gateway: <code>{snippet.gateway_url}</code> (your machine only). The snippet carries your
        member token — treat the file like a credential. Rotate it from Members if it leaks.
      </p>
      <CopySnippet text={rendered} />
    </div>
  );
}

function CopySnippet({ text }: { text: string }) {
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
    <button type="button" style={ui.primaryButton} onClick={() => void copy()}>
      {copied ? "Copied" : "Copy snippet"}
    </button>
  );
}
