/**
 * E21-T3 (MOD-13 onboarding, MOD-19 memory) — the first-run wizard.
 *
 * A new teammate's first ten minutes should end with a working project and a
 * connected agent (the hero flow's "join → first project → connect agent"
 * opening). The wizard walks three steps:
 *   1. Connect — paste the runtime token (reuses the Settings session flow).
 *   2. First project — name, goal, scope; on submit it creates the project AND
 *      seeds a project-brief memory entry (FR-E21-1) so the agent has context
 *      from the very first run. The brief is a `note` in the `project` space,
 *      linked to the new project (MOD-19 vocabularies — no new memory type).
 *   3. Connect your agent — points at the My Agent snippet (the one place the
 *      loopback-safe MCP snippet is rendered; the wizard guides, it does not
 *      duplicate that SEC surface).
 *
 * The brief is seeded `visibility: "team"`, so it syncs to teammates; the
 * MemoryService write path audits it like any other entry.
 */

import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { setToken, useSession } from "../lib/session";
import * as ui from "../lib/ui";

type Step = "connect" | "project" | "agent";

const STEPS: { id: Step; label: string }[] = [
  { id: "connect", label: "Connect" },
  { id: "project", label: "First project" },
  { id: "agent", label: "Connect your agent" },
];

/** The project-brief body the wizard seeds, composed from goal + scope. */
export function briefBody(name: string, goal: string, scope: string): string {
  const parts = [
    goal.trim() ? `Goal: ${goal.trim()}` : "",
    scope.trim() ? `Scope: ${scope.trim()}` : "",
  ].filter(Boolean);
  return parts.length > 0 ? parts.join("\n\n") : `Project brief for ${name.trim()}.`;
}

interface CreatedProject {
  id: string;
  name: string;
  briefSeeded: boolean;
}

export default function Onboarding() {
  const { connected } = useSession();
  const [created, setCreated] = useState<CreatedProject | null>(null);

  // The step follows the work done so far: not connected → connect; connected
  // but no project yet → project; project made → agent.
  const step: Step = !connected ? "connect" : created === null ? "project" : "agent";

  return (
    <section style={{ maxWidth: "40rem" }}>
      <h1>Welcome to kantaq</h1>
      <p style={ui.muted}>
        Three steps to your first working project and a connected agent. Your data stays on your
        machine; nothing an agent proposes applies until you approve it.
      </p>

      <StepRail current={step} />

      {step === "connect" && <ConnectStep />}
      {step === "project" && <ProjectStep onCreated={setCreated} />}
      {step === "agent" && created !== null && (
        <AgentStep project={created} briefSeeded={created.briefSeeded} />
      )}

      <p style={{ ...ui.muted, marginTop: "1.5rem" }}>
        Already set up? <Link to="/">Skip to the backlog</Link>.
      </p>
    </section>
  );
}

function StepRail({ current }: { current: Step }) {
  const currentIndex = STEPS.findIndex((s) => s.id === current);
  return (
    <ol
      aria-label="Onboarding steps"
      style={{
        display: "flex",
        gap: 8,
        listStyle: "none",
        padding: 0,
        margin: "1.25rem 0",
        flexWrap: "wrap",
      }}
    >
      {STEPS.map((s, index) => {
        const state = index < currentIndex ? "done" : index === currentIndex ? "current" : "ahead";
        return (
          <li
            key={s.id}
            aria-current={state === "current" ? "step" : undefined}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "0.25rem 0.7rem",
              borderRadius: ui.radius.pill,
              fontSize: ui.text.xs,
              fontWeight: 600,
              border: `1px solid ${state === "ahead" ? ui.palette.border : ui.palette.accent}`,
              background: state === "current" ? ui.palette.accent : ui.palette.raised,
              color:
                state === "current"
                  ? ui.palette.onAccent
                  : state === "ahead"
                    ? ui.palette.muted
                    : ui.palette.accent,
            }}
          >
            <span aria-hidden="true">{state === "done" ? "✓" : index + 1}</span>
            {s.label}
          </li>
        );
      })}
    </ol>
  );
}

function ConnectStep() {
  const [draft, setDraft] = useState("");

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setToken(draft);
    setDraft("");
  }

  return (
    <div style={ui.card}>
      <h2 style={{ marginTop: 0, fontSize: "1rem" }}>Connect to your runtime</h2>
      <p style={ui.muted}>
        Start the runtime on this machine, then paste its token (<code>kantaq token show</code>).
        The browser only ever talks to your own <code>127.0.0.1</code> runtime.
      </p>
      <form onSubmit={connect} style={{ display: "flex", gap: 8, alignItems: "end" }}>
        <label style={ui.label}>
          Runtime token
          <input
            type="password"
            style={{ ...ui.input, minWidth: "18rem" }}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            autoComplete="off"
          />
        </label>
        <button type="submit" style={ui.primaryButton}>
          Connect
        </button>
      </form>
    </div>
  );
}

function ProjectStep({ onCreated }: { onCreated: (project: CreatedProject) => void }) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [scope, setScope] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("give your project a name");
      return;
    }
    setBusy(true);
    setError(null);

    const { data: project, error: projectError } = await api.POST("/v1/projects", {
      body: { name: trimmed, goal: goal.trim(), scope: scope.trim(), status: "active" },
    });
    if (projectError !== undefined || project === undefined) {
      setBusy(false);
      setError("could not create the project");
      return;
    }

    // Seed the project-brief memory so the agent has context on its first run
    // (FR-E21-1). A failed seed should not strand the user — the project is
    // already created — so we advance with a soft warning instead of blocking.
    const { error: memoryError } = await api.POST("/v1/memory", {
      body: {
        title: `${trimmed} — project brief`,
        body: briefBody(trimmed, goal, scope),
        type: "note",
        source: "manual",
        space: "project",
        visibility: "team",
        confidence: "medium",
        linked_entities: [`projects/${project.id}`],
      },
    });
    setBusy(false);
    onCreated({ id: project.id, name: project.name, briefSeeded: memoryError === undefined });
  }

  return (
    <div style={ui.card}>
      <h2 style={{ marginTop: 0, fontSize: "1rem" }}>Create your first project</h2>
      <p style={ui.muted}>
        The goal and scope you write here become a seeded project brief in memory — the first
        context your agent reads.
      </p>
      <form onSubmit={submit} style={{ display: "grid", gap: 12, maxWidth: "32rem" }}>
        <label style={ui.label}>
          Project name
          <input
            style={ui.input}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. Apollo"
          />
        </label>
        <label style={ui.label}>
          Goal
          <textarea
            style={{ ...ui.input, minHeight: "3rem", resize: "vertical" }}
            value={goal}
            onChange={(event) => setGoal(event.target.value)}
            placeholder="what this project is trying to achieve"
          />
        </label>
        <label style={ui.label}>
          Scope
          <textarea
            style={{ ...ui.input, minHeight: "3rem", resize: "vertical" }}
            value={scope}
            onChange={(event) => setScope(event.target.value)}
            placeholder="what's in and what's out"
          />
        </label>
        <div>
          <button type="submit" style={ui.primaryButton} disabled={busy}>
            {busy ? "Creating…" : "Create project & seed brief"}
          </button>
        </div>
        {error !== null && <p style={ui.errorText}>{error}</p>}
      </form>
    </div>
  );
}

function AgentStep({ project, briefSeeded }: { project: CreatedProject; briefSeeded: boolean }) {
  return (
    <div style={ui.card}>
      <h2 style={{ marginTop: 0, fontSize: "1rem" }}>Connect your agent</h2>
      <p>
        <strong>{project.name}</strong> is ready
        {briefSeeded ? ", and its brief is in memory" : ""}. Now connect your coding agent so it can
        read tickets and propose changes.
      </p>
      {!briefSeeded && (
        <p style={ui.muted}>
          The project brief could not be seeded — you can add it later from Memory.
        </p>
      )}
      <p style={ui.muted}>
        My Agent generates a loopback MCP snippet for <strong>your</strong> runtime. Save it as{" "}
        <code>.mcp.json</code> in your project and your agent connects on start. Nothing it proposes
        applies until you approve it from the Inbox.
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: "0.5rem" }}>
        <Link to="/settings/my-agent" style={{ ...ui.primaryButton, textDecoration: "none" }}>
          Open the connection snippet →
        </Link>
        <Link to="/" style={{ ...ui.button, textDecoration: "none" }}>
          Finish — go to the backlog
        </Link>
      </div>
    </div>
  );
}
