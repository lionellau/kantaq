/**
 * Deterministic API-shape builders for UI tests (the MOD-30 builder rule:
 * tests compose these instead of hand-writing setup). Shapes match the
 * generated OpenAPI types, so a contract change breaks the builders loudly.
 */

import type {
  Activity,
  AgentSession,
  AgentSnippet,
  AuditCall,
  Comment,
  Device,
  Grant,
  LinkedMemory,
  Me,
  Member,
  MemoryEntry,
  MemoryLink,
  Project,
  Proposal,
  Recommendation,
  SkillContainer,
  SkillMapping,
  SyncStatus,
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
    recommended_next_stages: ["discovery"],
    parent_id: null,
    created_by: "member-1",
    attachments: [],
    created_at: T0,
    updated_at: T0,
    sync_state: "committed",
    pending_proposals: 0,
    subticket_count: 0,
    relationship_count: 0,
    blocked: false,
    ...overrides,
  };
}

export function buildRecommendation(overrides: Partial<Recommendation> = {}): Recommendation {
  return {
    role: "code_agent",
    skill_container: "code-review",
    why: "At the Review stage, kantaq recommends a code_agent run Code review.",
    required_memory: ["codebase", "decision", "ticket", "project"],
    missing_memory: ["codebase"],
    expected_output: "a code review: correctness, security, and maintainability findings",
    mapped_tool: "an MCP-connected coding agent (e.g. Claude Code, Codex)",
    mcp_session_template: '# kantaq MCP\nrole_context_get(ticket="tick-1")',
    risk_level: "medium",
    confidence: "rule_match_strong",
    approval_rule: "read_only",
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

export function buildMe(overrides: Partial<Me> = {}): Me {
  return {
    member_id: "member-1",
    email: "owner@example.com",
    role: "Owner",
    scopes: [],
    workspace_id: "ws-1",
    workspace_name: "Acme Workspace",
    ...overrides,
  };
}

export function buildDevice(overrides: Partial<Device> = {}): Device {
  return {
    id: "dev-1",
    label: "local runtime",
    public_key: "a".repeat(64),
    member_id: "member-1",
    member_email: "owner@example.com",
    created_at: T0,
    revoked_at: null,
    active: true,
    is_current: true,
    ...overrides,
  };
}

export function buildSkillContainer(overrides: Partial<SkillContainer> = {}): SkillContainer {
  return {
    id: "skc-1",
    slug: "code-review",
    name: "Code review",
    recommended_roles: ["code_agent"],
    supported_stages: ["implementation"],
    required_input: "",
    expected_output: "findings",
    allowed_tools: [],
    default_write_mode: "propose",
    risk_level: "medium",
    ...overrides,
  };
}

export function buildSkillMapping(overrides: Partial<SkillMapping> = {}): SkillMapping {
  return {
    id: "skm-1",
    container_id: "skc-1",
    scope: "personal",
    provider: "anthropic",
    connection: "My Claude Code",
    status: "active",
    created_by: "member-1",
    ...overrides,
  };
}

export function buildSyncStatus(overrides: Partial<SyncStatus> = {}): SyncStatus {
  return {
    hub_mode: "local",
    backend_configured: false,
    pending_events: 0,
    committed_events: 0,
    total_events: 0,
    last_committed_at: null,
    ...overrides,
  };
}

export function buildGrant(overrides: Partial<Grant> = {}): Grant {
  return {
    id: "grant-1",
    subject: "member-1",
    issuer: "dev-1",
    resource: "workspace/main",
    verbs: ["tickets.read"],
    issued_at: 1_767_225_600,
    expires_at: 1_767_229_200,
    revoked_at: null,
    sig: "ab12",
    valid: true,
    reason: "ok",
    ...overrides,
  };
}

export function buildAgentSession(overrides: Partial<AgentSession> = {}): AgentSession {
  return {
    grant_id: "grant-1",
    owner_member_id: "agent-1",
    owner_email: "bot@example.com",
    owner_role: "Agent",
    resource: "workspace/main",
    verbs: ["tickets.read", "proposals.write"],
    write_mode: "propose_only",
    issued_at: 1_767_225_600,
    expires_at: 1_767_229_200,
    revoked_at: null,
    active: true,
    reason: "ok",
    ...overrides,
  };
}

export function buildAuditCall(overrides: Partial<AuditCall> = {}): AuditCall {
  return {
    id: "aud-1",
    actor_id: "agent-1",
    action: "tool.deny",
    object_ref: "tools/ticket_search",
    source: "mcp",
    created_at: T0,
    reason: "tool_allowlist",
    detail: "ticket_search is not in this session's allowlist",
    session_id: "sess-1",
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
  // The literal "${KANTAQ_MEMBER_TOKEN}" is the server's placeholder contract —
  // the page substitutes it client-side, so no token ever round-trips.
  const headers = { Authorization: "Bearer ${KANTAQ_MEMBER_TOKEN}" };
  const claudeConfig = { mcpServers: { kantaq: { type: "http", url, headers } } };
  const cursorConfig = { mcpServers: { kantaq: { url, headers } } };
  const codexConfig = {
    mcp_servers: { kantaq: { url, bearer_token_env_var: "KANTAQ_AGENT_TOKEN" } },
  };
  return {
    member_id: "member-1",
    gateway_url: url,
    gateway_live: true,
    token_placeholder: "${KANTAQ_MEMBER_TOKEN}",
    snippet: claudeConfig,
    clients: [
      {
        client: "claude_code",
        label: "Claude Code",
        config: claudeConfig,
        format: "mcp_json",
        text: JSON.stringify(claudeConfig, null, 2),
        save_as: ".mcp.json",
        setup: null,
        instructions: "save as .mcp.json",
      },
      {
        client: "cursor",
        label: "Cursor",
        config: cursorConfig,
        format: "mcp_json",
        text: JSON.stringify(cursorConfig, null, 2),
        save_as: ".cursor/mcp.json",
        setup: null,
        instructions: "save as .cursor/mcp.json",
      },
      {
        client: "codex",
        label: "Codex",
        config: codexConfig,
        format: "toml",
        text: `[mcp_servers.kantaq]\nurl = "${url}"\nbearer_token_env_var = "KANTAQ_AGENT_TOKEN"`,
        save_as: "~/.codex/config.toml",
        setup: "export KANTAQ_AGENT_TOKEN=${KANTAQ_MEMBER_TOKEN}",
        instructions: "add to ~/.codex/config.toml; export KANTAQ_AGENT_TOKEN",
      },
    ],
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
