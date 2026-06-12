import { type FormEvent, useState } from "react";
import { clearToken, setToken, useSession } from "../lib/session";

/**
 * Settings — for now, the session connection (E18-T3).
 *
 * The full Settings tree (workspace, identity, devices, sync, members) lands
 * in Epics E20/E21 (MOD-12/MOD-13). This page wires the one thing the rest of
 * the app needs first: a bearer token for the member's own local runtime.
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
      <p>Workspace, identity, devices, sync, and members land in Epics E20/E21 (MOD-12/MOD-13).</p>
    </section>
  );
}
