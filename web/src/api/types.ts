/** Named aliases for the generated OpenAPI schemas (D-08: never hand-edited). */

import type { components } from "./schema";

export type Project = components["schemas"]["ProjectOut"];
export type Ticket = components["schemas"]["TicketOut"];
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
export type Recommendation = components["schemas"]["RecommendationOut"];

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
