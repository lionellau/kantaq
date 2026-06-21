/** Named aliases for the generated OpenAPI schemas (D-08: never hand-edited). */

import type { components } from "./schema";

export type Project = components["schemas"]["ProjectOut"];
export type Ticket = components["schemas"]["TicketOut"];
export type TicketPatch = components["schemas"]["TicketPatch"];
export type Comment = components["schemas"]["CommentOut"];
export type Activity = components["schemas"]["ActivityOut"];
export type Attachment = components["schemas"]["AttachmentOut"];
export type Proposal = components["schemas"]["ProposalOut"];
export type Member = components["schemas"]["MemberOut"];
export type Me = components["schemas"]["MeOut"];
export type Device = components["schemas"]["DeviceOut"];
export type SyncStatus = components["schemas"]["SyncStatusOut"];
export type Grant = components["schemas"]["GrantOut"];
export type AgentSnippet = components["schemas"]["AgentSnippetOut"];
export type MemoryEntry = components["schemas"]["MemoryOut"];
export type MemoryLink = components["schemas"]["MemoryLinkOut"];
export type LinkedMemory = components["schemas"]["LinkedMemoryOut"];
export type TelemetryView = components["schemas"]["TelemetryOut"];
export type Relation = components["schemas"]["RelationOut"];
export type RelationInput = components["schemas"]["RelationIn"];
// E14 (MOD-20) — milestones + the ticket-membership input.
export type Milestone = components["schemas"]["MilestoneOut"];
export type MilestoneInput = components["schemas"]["MilestoneIn"];
export type MilestonePatchInput = components["schemas"]["MilestonePatch"];
export type Recommendation = components["schemas"]["RecommendationOut"];
export type AgentSession = components["schemas"]["AgentSessionOut"];
export type AuditCall = components["schemas"]["AuditEventOut"];
export type SkillContainer = components["schemas"]["SkillContainerOut"];
export type SkillMapping = components["schemas"]["SkillMappingOut"];
// E20-T5 — sync-conflict review (MOD-26 §B4) + the metrics surface (MOD-27).
export type Conflict = components["schemas"]["ConflictOut"];
export type ResolveResult = components["schemas"]["ResolveOut"];
export type WorkspaceMetrics = components["schemas"]["WorkspaceMetricsOut"];
export type ActorUsage = components["schemas"]["ActorUsageOut"];

// The resolve choices the conflict tab offers (mirrors the runtime's RESOLVE_CHOICES).
export const RESOLVE_CHOICES = ["keep-A", "keep-B", "new-value"] as const;

// E14 (MOD-20) milestone lifecycle; values validated server-side.
export const MILESTONE_STATUSES = ["active", "complete", "archived"] as const;

// Mirrors kantaq_core.skills.service (E17 / MOD-22); values validated server-side.
export const SKILL_MAPPING_SCOPES = ["personal", "workspace"] as const;
export const SKILL_MAPPING_STATUSES = ["active", "disabled"] as const;

// The domain vocabularies (mirrors kantaq_core.tracker.service; values are
// validated server-side — these drive the filter/create selects only).
export const TICKET_STATUSES = ["todo", "doing", "done"] as const;
export const TICKET_PRIORITIES = ["low", "medium", "high", "urgent"] as const;
// Mirrors kantaq_core.tracker.service.RELATIONSHIP_TYPES (E12-T3 / MOD-03).
export const RELATIONSHIP_TYPES = [
  "related",
  "blocked-by",
  "blocking",
  "duplicate",
  "caused-by",
] as const;

// Mirrors kantaq_core.memory.service (E13 / MOD-19).
export const MEMORY_TYPES = ["note", "decision", "constraint", "learning", "reference"] as const;
export const MEMORY_SPACES = [
  "workspace",
  "project",
  "ticket",
  "codebase",
  "decision",
  "release",
  "agent_run",
] as const;
export const MEMORY_CONFIDENCE = ["low", "medium", "high"] as const;
