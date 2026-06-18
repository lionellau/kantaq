import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import TokenShowHint from "../components/TokenShowHint";
import { clearToken, runtimeTokenProblem, setToken, useSession } from "../lib/session";
import * as ui from "../lib/ui";

/**
 * Settings — the session connection (E18-T3) and the full Settings tree
 * (E20-T2, MOD-12): Workspace, Identity, Devices, Sync, Export. Each node is
 * its own page; the workspace-level admin surfaces (Members, MOD-13; Telemetry,
 * MOD-25) and the agent-connection snippet (My Agent) nest under their parent
 * section so there is one obvious place for each setting.
 */

interface TreeNode {
  to: string;
  label: string;
  hint: string;
  children?: TreeNode[];
}

const TREE: TreeNode[] = [
  {
    to: "/settings/workspace",
    label: "Workspace",
    hint: "the shared workspace and its members",
    children: [
      { to: "/settings/members", label: "Members", hint: "invite, revoke, rotate tokens" },
      { to: "/settings/telemetry", label: "Telemetry", hint: "opt-in usage metrics" },
    ],
  },
  {
    to: "/settings/identity",
    label: "Identity",
    hint: "your member, role, and capability grants",
    children: [
      { to: "/settings/my-agent", label: "My Agent", hint: "connect your coding agent" },
      {
        to: "/settings/skill-mappings",
        label: "Skill mappings",
        hint: "map a skill to the tool you drive it with",
      },
    ],
  },
  { to: "/settings/devices", label: "Devices", hint: "registered signing keys (trust roots)" },
  { to: "/settings/sync", label: "Sync", hint: "backend mode and local event-log state" },
  { to: "/settings/export", label: "Export", hint: "take your workspace data with you" },
];

export default function Settings() {
  const { connected } = useSession();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = runtimeTokenProblem(draft);
    if (problem !== null) {
      setError(problem); // reject a wrong paste (e.g. a Supabase key) before it 401s
      return;
    }
    setError(null);
    setToken(draft);
    setDraft("");
  }

  return (
    <section>
      <h1>Settings</h1>

      <section aria-labelledby="session-heading">
        <h2 id="session-heading" style={ui.sectionHeading}>
          Session
        </h2>
        {connected ? (
          <>
            <p>
              <output>Connected to your local runtime.</output>
            </p>
            <button type="button" style={ui.button} onClick={() => clearToken()}>
              Disconnect
            </button>
          </>
        ) : (
          <>
            <p>
              <output>
                Not connected. Get your runtime token from the CLI, then paste it below.
              </output>
            </p>
            <div style={{ margin: "0 0 0.75rem" }}>
              <TokenShowHint />
            </div>
            <form onSubmit={connect} style={{ display: "flex", gap: 8, alignItems: "end" }}>
              <label style={ui.label}>
                Runtime token
                <input
                  type="password"
                  style={ui.input}
                  value={draft}
                  onChange={(event) => {
                    setDraft(event.target.value);
                    if (error !== null) {
                      setError(null);
                    }
                  }}
                  autoComplete="off"
                  aria-invalid={error !== null}
                />
              </label>
              <button type="submit" style={ui.primaryButton}>
                Connect
              </button>
            </form>
            {error !== null && (
              <p role="alert" style={{ ...ui.errorText, marginBottom: 0 }}>
                {error}
              </p>
            )}
          </>
        )}
      </section>

      <nav aria-label="Settings sections">
        <h2 style={ui.sectionHeading}>All settings</h2>
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 18 }}>
          {TREE.map((node) => (
            <li key={node.to}>
              <Link
                to={node.to}
                style={{
                  fontWeight: 600,
                  fontSize: "0.95rem",
                  color: ui.palette.text,
                  textDecoration: "none",
                }}
              >
                {node.label}
              </Link>
              <div style={{ ...ui.muted, marginTop: 2 }}>{node.hint}</div>
              {node.children && node.children.length > 0 && (
                <ul
                  style={{
                    listStyle: "none",
                    margin: "10px 0 0",
                    padding: "0 0 0 0.9rem",
                    borderLeft: `2px solid ${ui.palette.border}`,
                    display: "grid",
                    gap: 10,
                  }}
                >
                  {node.children.map((child) => (
                    <li key={child.to}>
                      <Link to={child.to} style={{ color: ui.palette.text }}>
                        {child.label}
                      </Link>
                      <div style={{ ...ui.muted, marginTop: 2 }}>{child.hint}</div>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      </nav>
    </section>
  );
}
