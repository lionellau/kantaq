import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { clearToken, setToken, useSession } from "../lib/session";
import * as ui from "../lib/ui";

/**
 * Settings — the session connection (E18-T3) plus the E21 subpages:
 * Members (invite/list/revoke/rotate) and My Agent (the loopback MCP
 * snippet). The full Settings tree (workspace, identity, devices, sync,
 * export) is the E20-T2 stretch and lands later.
 */
export default function Settings() {
  const { connected } = useSession();
  const [draft, setDraft] = useState("");

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setToken(draft);
    setDraft("");
  }

  return (
    <section>
      <h1>Settings</h1>
      <section aria-labelledby="session-heading">
        <h2 id="session-heading">Session</h2>
        {connected ? (
          <>
            <p>
              <output>Connected to your local runtime.</output>
            </p>
            <button type="button" onClick={() => clearToken()}>
              Disconnect
            </button>
          </>
        ) : (
          <>
            <p>
              <output>
                Not connected. Paste your runtime token (<code>kantaq token show</code>).
              </output>
            </p>
            <form onSubmit={connect}>
              <label>
                Runtime token{" "}
                <input
                  type="password"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  autoComplete="off"
                />
              </label>
              <button type="submit">Connect</button>
            </form>
          </>
        )}
      </section>
      <nav aria-label="Settings sections">
        <h2 style={ui.sectionHeading}>Workspace</h2>
        <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 4 }}>
          <li>
            <Link to="/settings/members">Members</Link>
            <span style={ui.muted}> — invite, revoke, rotate tokens</span>
          </li>
          <li>
            <Link to="/settings/my-agent">My Agent</Link>
            <span style={ui.muted}> — connect your coding agent to this runtime</span>
          </li>
          <li>
            <Link to="/settings/telemetry">Telemetry</Link>
            <span style={ui.muted}> — opt-in usage metrics, inspect what is collected</span>
          </li>
        </ul>
      </nav>
      <p style={ui.muted}>
        Workspace, identity, devices, sync, and export settings land with the v0.1 Settings tree
        (MOD-12).
      </p>
    </section>
  );
}
