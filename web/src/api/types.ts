/** Named aliases for the generated OpenAPI schemas (D-08: never hand-edited). */

import type { components } from "./schema";

export type Project = components["schemas"]["ProjectOut"];
export type Ticket = components["schemas"]["TicketOut"];
export type Comment = components["schemas"]["CommentOut"];
export type Activity = components["schemas"]["ActivityOut"];
export type Attachment = components["schemas"]["AttachmentOut"];
export type Proposal = components["schemas"]["ProposalOut"];
export type Member = components["schemas"]["MemberOut"];
export type AgentSnippet = components["schemas"]["AgentSnippetOut"];

// The domain vocabularies (mirrors kantaq_core.tracker.service; values are
// validated server-side — these drive the filter/create selects only).
export const TICKET_STATUSES = ["todo", "doing", "done"] as const;
export const TICKET_PRIORITIES = ["low", "medium", "high", "urgent"] as const;
