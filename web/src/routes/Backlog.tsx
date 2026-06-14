/**
 * E19-T1 (MOD-11) — the backlog list.
 *
 * One filterable table over `/v1/tickets`: project, status, assignee, label,
 * lifecycle stage (FR-E19-1). Each row carries the sync badge and the
 * pending-proposal chip and links to its ticket page. Refreshes on the 2 s
 * poll (MOD-14) so teammates' synced changes appear without a reload. A
 * compact create form covers the hero flow's "create a ticket" step.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import {
  type Member,
  type Project,
  TICKET_PRIORITIES,
  TICKET_STATUSES,
  type Ticket,
} from "../api/types";
import ProposalChip from "../components/ProposalChip";
import SyncBadge, { type SyncState } from "../components/SyncBadge";
import { fmtDateTime } from "../lib/format";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";

interface Filters {
  project: string;
  status: string;
  assignee: string;
  label: string;
  stage: string;
}

const NO_FILTERS: Filters = { project: "", status: "", assignee: "", label: "", stage: "" };

export default function Backlog() {
  const { connected } = useSession();
  const [projects, setProjects] = useState<Project[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const query: Record<string, string> = {};
    if (filters.project) query.project = filters.project;
    if (filters.status) query.status = filters.status;
    if (filters.assignee) query.assignee = filters.assignee;
    if (filters.label) query.label = filters.label;
    if (filters.stage) query.stage = filters.stage;
    const { data, error: apiError } = await api.GET("/v1/tickets", { params: { query } });
    if (apiError !== undefined) {
      setError("could not load tickets");
      return;
    }
    setError(null);
    setTickets(data);
  }, [connected, filters]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(refresh, 2000, connected);

  useEffect(() => {
    if (!connected) {
      return;
    }
    void api.GET("/v1/projects").then(({ data }) => setProjects(data ?? []));
    void api.GET("/v1/members").then(({ data }) => setMembers(data ?? []));
  }, [connected]);

  function setFilter(key: keyof Filters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  if (!connected) {
    return (
      <section>
        <h1>Backlog</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1>Backlog</h1>

      {projects.length === 0 && (
        <p
          style={{
            ...ui.card,
            background: ui.palette.surface,
            margin: "0 0 1rem",
          }}
        >
          New here? <Link to="/onboarding">Start the setup wizard</Link> to create your first
          project and connect your agent.
        </p>
      )}

      <form aria-label="Filters" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <label style={ui.label}>
          Project
          <select
            style={ui.input}
            value={filters.project}
            onChange={(event) => setFilter("project", event.target.value)}
          >
            <option value="">All</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Status
          <select
            style={ui.input}
            value={filters.status}
            onChange={(event) => setFilter("status", event.target.value)}
          >
            <option value="">All</option>
            {TICKET_STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Assignee
          <select
            style={ui.input}
            value={filters.assignee}
            onChange={(event) => setFilter("assignee", event.target.value)}
          >
            <option value="">Anyone</option>
            {members.map((member) => (
              <option key={member.id} value={member.id}>
                {member.email}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Label
          <input
            style={ui.input}
            value={filters.label}
            onChange={(event) => setFilter("label", event.target.value)}
            placeholder="any label"
          />
        </label>
        <label style={ui.label}>
          Stage
          <input
            style={ui.input}
            value={filters.stage}
            onChange={(event) => setFilter("stage", event.target.value)}
            placeholder="any stage"
          />
        </label>
      </form>

      <CreateTicket projects={projects} onCreated={() => void refresh()} />

      {error !== null && <p style={ui.errorText}>{error}</p>}
      {tickets !== null && tickets.length === 0 && <p style={ui.muted}>No tickets match.</p>}
      {tickets !== null && tickets.length > 0 && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.th}>Title</th>
              <th style={ui.th}>Status</th>
              <th style={ui.th}>Priority</th>
              <th style={ui.th}>Assignee</th>
              <th style={ui.th}>Project</th>
              <th style={ui.th}>Updated</th>
              <th style={ui.th}>Sync</th>
            </tr>
          </thead>
          <tbody>
            {tickets.map((ticket) => (
              <tr key={ticket.id}>
                <td style={ui.td}>
                  <Link to={`/tickets/${ticket.id}`} style={{ fontWeight: 600 }}>
                    {ticket.title}
                  </Link>
                </td>
                <td style={ui.td}>{ticket.status}</td>
                <td style={ui.td}>{ticket.priority}</td>
                <td style={ui.td}>{memberEmail(members, ticket.assignee) ?? "—"}</td>
                <td style={ui.td}>{projectName(projects, ticket.project_id)}</td>
                <td style={ui.td}>{fmtDateTime(ticket.updated_at)}</td>
                <td style={{ ...ui.td, whiteSpace: "nowrap" }}>
                  <SyncBadge state={ticket.sync_state as SyncState} />{" "}
                  <ProposalChip count={ticket.pending_proposals} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function projectName(projects: Project[], id: string): string {
  return projects.find((project) => project.id === id)?.name ?? id;
}

function memberEmail(members: Member[], id: string | null): string | null {
  if (id === null) {
    return null;
  }
  return members.find((member) => member.id === id)?.email ?? id;
}

function CreateTicket({
  projects,
  onCreated,
}: {
  projects: Project[];
  onCreated: () => void;
}) {
  const [title, setTitle] = useState("");
  const [projectId, setProjectId] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [priority, setPriority] = useState("medium");
  const [error, setError] = useState<string | null>(null);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let project = projectId || projects[0]?.id;
    if (!project && newProjectName.trim()) {
      // First run: no project exists yet, so the form creates one inline.
      const { data, error: projectError } = await api.POST("/v1/projects", {
        body: { name: newProjectName.trim(), goal: "", scope: "", status: "active" },
      });
      if (projectError !== undefined || data === undefined) {
        setError("could not create the project");
        return;
      }
      project = data.id;
    }
    if (!project || !title.trim()) {
      setError("pick a project and a title");
      return;
    }
    const { error: apiError } = await api.POST("/v1/tickets", {
      body: {
        project_id: project,
        title: title.trim(),
        priority,
        description: "",
        acceptance_criteria: "",
        status: "todo",
        lifecycle_stage: "intake",
      },
    });
    if (apiError !== undefined) {
      setError("could not create the ticket");
      return;
    }
    setError(null);
    setTitle("");
    onCreated();
  }

  return (
    <form
      aria-label="Create ticket"
      onSubmit={create}
      style={{ display: "flex", gap: 8, alignItems: "end", margin: "1rem 0", flexWrap: "wrap" }}
    >
      <label style={ui.label}>
        Title
        <input
          style={{ ...ui.input, minWidth: "16rem" }}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="ticket title"
        />
      </label>
      {projects.length > 0 ? (
        <label style={ui.label}>
          In project
          <select
            style={ui.input}
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <label style={ui.label}>
          First project
          <input
            style={ui.input}
            value={newProjectName}
            onChange={(event) => setNewProjectName(event.target.value)}
            placeholder="project name"
          />
        </label>
      )}
      <label style={ui.label}>
        Priority
        <select
          style={ui.input}
          value={priority}
          onChange={(event) => setPriority(event.target.value)}
        >
          {TICKET_PRIORITIES.map((value) => (
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
