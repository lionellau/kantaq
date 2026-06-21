/**
 * E14-T3 (MOD-20/MOD-11) — the Milestones page.
 *
 * One table over `/v1/milestones` (optionally scoped to a project) with each
 * milestone's target date, status, and ticket count (the count is batched
 * server-side, no N+1). A compact create form, and per-row complete/archive/
 * delete actions for Member+ roles. Refreshes on the 2 s poll (MOD-14) like the
 * rest of the shell. Flat milestones (nestable is deferred to Sprint 10+).
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { MILESTONE_STATUSES, type Milestone, type Project } from "../api/types";
import { fmtDateTime } from "../lib/format";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

function StatusChip({ status }: { status: string }) {
  const done = status === "complete";
  const archived = status === "archived";
  const style = done
    ? { ...ui.chip, background: "#dcfce7", color: "#166534" }
    : archived
      ? { ...ui.chip, color: ui.palette.muted }
      : ui.chip;
  return <span style={style}>{status}</span>;
}

export default function Milestones() {
  const { connected } = useSession();
  const [milestones, setMilestones] = useState<Milestone[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectFilter, setProjectFilter] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const query: Record<string, string> = {};
    if (projectFilter) query.project_id = projectFilter;
    const { data, error: apiError } = await api.GET("/v1/milestones", { params: { query } });
    if (apiError !== undefined) {
      setError("could not load milestones");
      return;
    }
    setError(null);
    setMilestones(data);
  }, [connected, projectFilter]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(refresh, 2000, connected);

  useEffect(() => {
    if (!connected) {
      return;
    }
    void api.GET("/v1/projects").then(({ data }) => setProjects(data ?? []));
  }, [connected]);

  async function setStatus(milestone: Milestone, status: string) {
    const { error: apiError } = await api.PATCH("/v1/milestones/{milestone_id}", {
      params: { path: { milestone_id: milestone.id } },
      body: { status },
    });
    if (apiError !== undefined) {
      setError("could not update the milestone");
      return;
    }
    void refresh();
  }

  async function remove(milestone: Milestone) {
    const { error: apiError } = await api.DELETE("/v1/milestones/{milestone_id}", {
      params: { path: { milestone_id: milestone.id } },
    });
    if (apiError !== undefined) {
      setError("could not delete the milestone");
      return;
    }
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Milestones</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1>Milestones</h1>
      <p style={ui.muted}>Group tickets under a target-dated milestone and track its progress.</p>

      <form aria-label="Milestone filters" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <label style={ui.label}>
          Project
          <select
            style={ui.input}
            value={projectFilter}
            onChange={(event) => setProjectFilter(event.target.value)}
          >
            <option value="">All projects</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
      </form>

      <CreateMilestone projects={projects} onCreated={() => void refresh()} />

      {error !== null && <p style={ui.errorText}>{error}</p>}
      {milestones !== null && milestones.length === 0 && <p style={ui.muted}>No milestones yet.</p>}
      {milestones !== null && milestones.length > 0 && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.th}>Name</th>
              <th style={ui.th}>Target date</th>
              <th style={ui.th}>Status</th>
              <th style={ui.th}>Tickets</th>
              <th style={ui.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {milestones.map((milestone) => (
              <tr key={milestone.id}>
                <td style={ui.td}>
                  <span style={{ fontWeight: 600 }}>{milestone.name}</span>
                  {milestone.description.trim() !== "" && (
                    <div style={{ ...ui.muted, maxWidth: "28rem" }}>{milestone.description}</div>
                  )}
                </td>
                <td style={ui.td}>
                  {milestone.target_date !== null ? fmtDateTime(milestone.target_date) : "—"}
                </td>
                <td style={ui.td}>
                  <StatusChip status={milestone.status} />
                </td>
                <td style={ui.td}>{milestone.ticket_count}</td>
                <td style={{ ...ui.td, whiteSpace: "nowrap" }}>
                  {milestone.status !== "complete" && (
                    <button
                      type="button"
                      style={ui.button}
                      onClick={() => void setStatus(milestone, "complete")}
                    >
                      Complete
                    </button>
                  )}{" "}
                  {milestone.status !== "archived" && (
                    <button
                      type="button"
                      style={ui.button}
                      onClick={() => void setStatus(milestone, "archived")}
                    >
                      Archive
                    </button>
                  )}{" "}
                  <button
                    type="button"
                    style={ui.dangerButton}
                    onClick={() => void remove(milestone)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function CreateMilestone({
  projects,
  onCreated,
}: {
  projects: Project[];
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [projectId, setProjectId] = useState("");
  const [targetDate, setTargetDate] = useState("");
  const [status, setStatus] = useState("active");
  const [error, setError] = useState<string | null>(null);

  const effectiveProject = projectId || projects[0]?.id || "";

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim()) {
      setError("a milestone needs a name");
      return;
    }
    if (!effectiveProject) {
      setError("create a project first");
      return;
    }
    const { error: apiError } = await api.POST("/v1/milestones", {
      body: {
        project_id: effectiveProject,
        name: name.trim(),
        description: "",
        target_date: targetDate ? new Date(targetDate).toISOString() : null,
        status,
      },
    });
    if (apiError !== undefined) {
      setError("could not create the milestone");
      return;
    }
    setError(null);
    setName("");
    setTargetDate("");
    onCreated();
  }

  return (
    <form
      aria-label="Create milestone"
      onSubmit={create}
      style={{ display: "flex", gap: 8, alignItems: "end", margin: "1rem 0", flexWrap: "wrap" }}
    >
      <label style={ui.label}>
        Name
        <input
          style={{ ...ui.input, minWidth: "14rem" }}
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="e.g. v1.0 launch"
        />
      </label>
      <label style={ui.label}>
        Project
        <select
          style={ui.input}
          value={effectiveProject}
          onChange={(event) => setProjectId(event.target.value)}
        >
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>
      <label style={ui.label}>
        Target date
        <input
          type="date"
          style={ui.input}
          value={targetDate}
          onChange={(event) => setTargetDate(event.target.value)}
        />
      </label>
      <label style={ui.label}>
        Status
        <select style={ui.input} value={status} onChange={(event) => setStatus(event.target.value)}>
          {MILESTONE_STATUSES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" style={ui.primaryButton}>
        Create
      </button>
      {error !== null && <span style={ui.errorText}>{error}</span>}
    </form>
  );
}
