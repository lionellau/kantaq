/**
 * Deterministic API-shape builders for UI tests (the MOD-30 builder rule:
 * tests compose these instead of hand-writing setup). Shapes match the
 * generated OpenAPI types, so a contract change breaks the builders loudly.
 */

import type {
  Activity,
  AgentSnippet,
  Comment,
  LinkedMemory,
  Member,
  MemoryEntry,
  MemoryLink,
  Project,
  Proposal,
  TelemetryView,
  Ticket,
} from "../api/types";

const T0 = "2026-01-01T00:00:00";

export function buildProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "proj-1",
    workspace_id: "ws-1",
    name: "Apollo",
    goal: "",
    scope: "",
    owner: null,
    target_date: null,
    status: "active",
    created_at: T0,
    updated_at: T0,
    ...overrides,
  };
}

export function buildTicket(overrides: Partial<Ticket> = {}): Ticket {
  return {
    id: "tick-1",
    project_id: "proj-1",
    title: "Fix the flux capacitor",
    description: "",
    status: "todo",
    priority: "medium",
    labels: [],
    assignee: null,
    due_date: null,
    acceptance_criteria: "",
    lifecycle_stage: "intake",
    parent_id: null,
    created_by: "member-1",
    attachments: [],
    created_at: T0,
    updated_at: T0,
    sync_state: "committed",
    pending_proposals: 0,
    ...overrides,
  };
}

export function buildComment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: "comm-1",
    ticket_id: "tick-1",
    author_actor_id: "member-1",
    body: "Looks good.",
    created_at: T0,
    ...overrides,
  };
}

export function buildActivity(overrides: Partial<Activity> = {}): Activity {
  return {
    id: "act-1",
    actor_id: "member-1",
    action: "ticket.update",
    before: { status: "todo" },
    after: { status: "doing" },
    created_at: T0,
    ...overrides,
  };
}

export function buildProposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: "prop-1",
    ticket_id: "tick-1",
    ticket_title: "Fix the flux capacitor",
    proposer_id: "agent-1",
    status: "pending",
    diff: { changes: { status: "doing" }, note: "ready to start" },
    created_at: T0,
    updated_at: T0,
    ...overrides,
  };
}

export function buildMember(overrides: Partial<Member> = {}): Member {
  return {
    id: "member-1",
    workspace_id: "ws-1",
    email: "owner@example.com",
    role: "Owner",
    status: "active",
    created_at: T0,
    ...overrides,
  };
}

export function buildMemoryEntry(overrides: Partial<MemoryEntry> = {}): MemoryEntry {
  return {
    id: "mem-1",
    title: "Sync design decision",
    body: "We fold events in commit order.",
    type: "decision",
    source: "manual",
    space: "codebase",
    linked_entities: [],
    provenance: { origin: "manual", actor_id: "member-1", captured_at: T0 },
    confidence: "high",
    review_status: "draft",
    visibility: "team",
    domain_visibility: "personal_synced",
    expires_at: null,
    created_by: "member-1",
    created_at: T0,
    updated_at: T0,
    ...overrides,
  };
}

export function buildMemoryLink(overrides: Partial<MemoryLink> = {}): MemoryLink {
  return {
    id: "mlink-1",
    ticket_id: "tick-1",
    memory_id: "mem-1",
    reason: "explains the design",
    visibility: "team",
    created_by: "member-1",
    created_at: T0,
    ...overrides,
  };
}

export function buildLinkedMemory(overrides: Partial<LinkedMemory> = {}): LinkedMemory {
  return {
    link: buildMemoryLink(),
    entry: buildMemoryEntry(),
    ...overrides,
  };
}

export function buildSnippet(overrides: Partial<AgentSnippet> = {}): AgentSnippet {
  const url = "http://127.0.0.1:54321/v1/mcp";
  return {
    member_id: "member-1",
    gateway_url: url,
    gateway_live: true,
    token_placeholder: "${KANTAQ_MEMBER_TOKEN}",
    snippet: {
      mcpServers: {
        kantaq: {
          type: "http",
          url,
          // The literal "${KANTAQ_MEMBER_TOKEN}" is the server's placeholder
          // contract — the page substitutes it client-side.
          headers: { Authorization: "Bearer ${KANTAQ_MEMBER_TOKEN}" },
        },
      },
    },
    instructions: "save as .mcp.json",
    ...overrides,
  };
}

type TelemetryViewOverrides = Partial<Omit<TelemetryView, "metrics">> & {
  metrics?: Partial<TelemetryView["metrics"]>;
};

export function buildTelemetryView(overrides: TelemetryViewOverrides = {}): TelemetryView {
  const { metrics, ...rest } = overrides;
  return {
    enabled: false,
    events: [],
    ...rest,
    metrics: {
      events_total: 0,
      proposal_acceptance_rate: null,
      median_seconds_to_approve: null,
      mcp_sessions_total: 0,
      repeat_session_members: 0,
      activity_views_total: 0,
      install_to_first_proposal_seconds: null,
      weekly_active: false,
      ...(metrics ?? {}),
    },
  };
}
