"""The v0.0.5 MCP tools: ``ticket_get`` and ``agent_action_propose`` (MOD-09).

Tools are pure over the gateway (NFR-E10-1): they never see a raw HTTP
request, never check permissions (the gateway's checks ran already), and call
``kantaq_core`` / the event log exactly like the runtime's own write path.
Each handler is bound to one DB session and one acting member; write handlers
commit their own transaction so the domain row, its audit row, and its event
ride one commit (the MOD-07 write-path contract).

Every human-authored string a tool returns is wrapped by
``kantaq_mcp.security.tag_untrusted`` (FR-E10-4): title, description,
acceptance criteria, labels, assignee, attachment filenames. Validated enums
(status, priority, lifecycle stage), ULIDs, and timestamps are returned raw —
they are produced by the domain, not by humans.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlmodel import Session

from kantaq_core import audit, context, memory_policy, proposals
from kantaq_core.memory.service import (
    MemoryNotFoundError,
    MemoryService,
    MemoryValidationError,
)
from kantaq_core.memory_policy import MemoryPolicy
from kantaq_core.tracker.events import DomainEvent
from kantaq_core.tracker.service import (
    FOLLOW_UP_STATUSES,
    TICKET_PRIORITIES,
    TICKET_STATUSES,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
)
from kantaq_db.models import AgentProposal, FollowUp, MemoryEntry, Project, Ticket, Workspace
from kantaq_mcp.security import tag_untrusted
from kantaq_sync_engine.log import EventLogSink, EventSigner


@dataclass(frozen=True)
class ToolScope:
    """The session-resolved scope the gateway hands every tool.

    The gateway derives this from the session before dispatch so tools never
    touch session or permission state themselves (NFR-E10-1 — pure over the
    gateway). ``memory_policy`` is set only for an agent context session; a
    role-less *human* session reads memory unfiltered (its base role/RLS already
    governs that), while a role-less *agent* session is denied memory reads
    (``is_agent`` with no policy — fail closed). ``signer`` (E04-T4) is the
    device signer the gateway resolves for **write** verbs so a tool's emitted
    events are signed past the cutover (``None`` pre-cutover / for reads). The
    default :data:`UNSCOPED` is the no-restriction scope used by direct handler
    calls in tests.
    """

    agent_role: str | None = None
    memory_policy: MemoryPolicy | None = None
    is_agent: bool = False
    signer: EventSigner | None = None


UNSCOPED = ToolScope()

# The ticket fields a proposal may change. Deliberately a local mirror of the
# tracker's patch allowlist (MOD-03 owns that module): a contract test pins
# this set against ``kantaq_core.tracker.service._TICKET_PATCHABLE`` so drift
# fails loudly instead of silently widening what agents can propose.
PROPOSABLE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "status",
        "priority",
        "labels",
        "assignee",
        "due_date",
        "acceptance_criteria",
        "lifecycle_stage",
        "parent_id",
    }
)

_NOTE_MAX = 2_000


def _ticket_id(args: dict[str, Any]) -> str:
    """The SDK validates input schemas, but its tool cache is best-effort —
    never trust that validation actually ran (fail closed on garbage)."""
    ticket_id = args.get("ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        raise ToolError("validation", "ticket_id (string) is required")
    return ticket_id


def _milestone_id(args: dict[str, Any]) -> str:
    """Fail closed on a missing/garbage milestone_id (same discipline as _ticket_id)."""
    milestone_id = args.get("milestone_id")
    if not isinstance(milestone_id, str) or not milestone_id.strip():
        raise ToolError("validation", "milestone_id (string) is required")
    return milestone_id


class ToolError(Exception):
    """A domain-level tool failure, returned to the agent as a structured error.

    Not a gateway denial: the call was permitted, the domain said no (unknown
    ticket, invalid proposal). Nothing is persisted when this raises.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PolicyDenied(Exception):
    """A read the session's memory policy forbids (the 8-check check 6).

    Raised by a memory-read tool when the scope's policy excludes the requested
    entry. Distinct from :class:`ToolError`: the gateway catches it, writes a
    ``tool.deny`` (reason ``memory_policy``), and returns a structured denial —
    a *check failure*, not a domain error, so the agent cannot tell a
    policy-withheld entry from a missing one (no existence leak).
    """

    def __init__(self, message: str, *, entry_id: str, reason: str) -> None:
        super().__init__(message)
        self.entry_id = entry_id
        self.reason = reason


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def ticket_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Read one ticket; every human-authored string comes back fenced untrusted."""
    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        ticket = service.get_ticket(_ticket_id(args))
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc

    return {
        "ticket": {
            "id": ticket.id,
            "project_id": ticket.project_id,
            "title": tag_untrusted(ticket.title, "ticket.title"),
            "description": tag_untrusted(ticket.description, "ticket.description"),
            "status": ticket.status,
            "priority": ticket.priority,
            "labels": [tag_untrusted(label, "ticket.label") for label in ticket.labels],
            "assignee": (
                tag_untrusted(ticket.assignee, "ticket.assignee")
                if ticket.assignee is not None
                else None
            ),
            "due_date": _iso(ticket.due_date),
            "acceptance_criteria": tag_untrusted(
                ticket.acceptance_criteria, "ticket.acceptance_criteria"
            ),
            "lifecycle_stage": ticket.lifecycle_stage,
            "parent_id": ticket.parent_id,
            "created_by": ticket.created_by,
            "created_at": ticket.created_at.isoformat(),
            "updated_at": ticket.updated_at.isoformat(),
            "attachments": [
                {
                    "blob_id": str(ref.get("blob_id", "")),
                    "filename": tag_untrusted(str(ref.get("filename", "")), "attachment.filename"),
                    "media_type": str(ref.get("media_type", "")),
                    "size_bytes": int(ref.get("size_bytes", 0)),
                }
                for ref in ticket.attachments
            ],
        }
    }


def milestone_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Read one milestone by id; its name + description come back fenced untrusted.

    The milestone's grouped ticket ids are included so an agent can pull a
    milestone's scope in one read. Read-only (verb ``read``); the gateway's
    eight checks have already authorized ``tickets.read`` before dispatch.
    """
    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        milestone = service.get_milestone(_milestone_id(args))
        ticket_ids = [t.id for t in service.tickets_for_milestone(milestone.id)]
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc

    return {
        "milestone": {
            "id": milestone.id,
            "project_id": milestone.project_id,
            "name": tag_untrusted(milestone.name, "milestone.name"),
            "description": tag_untrusted(milestone.description, "milestone.description"),
            "target_date": _iso(milestone.target_date),
            "status": milestone.status,
            "created_by": milestone.created_by,
            "created_at": milestone.created_at.isoformat(),
            "updated_at": milestone.updated_at.isoformat(),
            "ticket_ids": ticket_ids,
            "ticket_count": len(ticket_ids),
        }
    }


def _validated_changes(changes: Any) -> dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise ToolError("validation", "changes must be a non-empty object of ticket fields")
    unknown = set(changes) - PROPOSABLE_FIELDS
    if unknown:
        raise ToolError(
            "validation",
            f"fields not proposable: {sorted(unknown)}; allowed: {sorted(PROPOSABLE_FIELDS)}",
        )
    if "status" in changes and changes["status"] not in TICKET_STATUSES:
        raise ToolError(
            "validation",
            f"unknown status {changes['status']!r}; expected one of {TICKET_STATUSES}",
        )
    if "priority" in changes and changes["priority"] not in TICKET_PRIORITIES:
        raise ToolError(
            "validation",
            f"unknown priority {changes['priority']!r}; expected one of {TICKET_PRIORITIES}",
        )
    return dict(changes)


def agent_action_propose(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Store an ``agent_proposal`` for a ticket change. Never touches the ticket.

    The proposal row syncs like any collection (so it reaches every member's
    Inbox); the proposed change itself is applied only when a human approves it
    from the Inbox (MOD-12, E20) — full value validation happens there, at
    apply time, through the same tracker rules as any human write.
    """
    note = str(args.get("note", ""))
    if len(note) > _NOTE_MAX:
        raise ToolError("validation", f"note exceeds {_NOTE_MAX} characters")
    changes = _validated_changes(args.get("changes"))

    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        ticket = service.get_ticket(_ticket_id(args))
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc

    ts = now()
    proposal = AgentProposal(
        ticket_id=ticket.id,
        proposer_id=actor_id,
        diff={"changes": changes, "note": note},
        status="pending",
        created_at=ts,
        updated_at=ts,
    )
    session.add(proposal)
    session.flush()
    # Agent writes are always audited in detail (PRD §8.6); the proposal row,
    # its audit row, and its sync event commit atomically (MOD-07 contract).
    audit.write(
        session,
        actor_id=actor_id,
        action="proposal.create",
        source="mcp",
        object_ref=f"agent_proposals/{proposal.id}",
        after=audit.snapshot(proposal),
        now=ts,
    )
    EventLogSink(session, actor_id, signer=scope.signer).emit(
        DomainEvent(
            collection="agent_proposals",
            entity_id=proposal.id,
            op="patch",
            payload=audit.snapshot(proposal),
        )
    )
    session.commit()
    session.refresh(proposal)

    return {
        "proposal": {
            "id": proposal.id,
            "ticket_id": proposal.ticket_id,
            "proposer_id": proposal.proposer_id,
            "status": proposal.status,
            "diff": proposal.diff,
            "created_at": proposal.created_at.isoformat(),
        },
        "applied": False,
    }


# -------------------------------------------------- v0.3 follow-up tools (MOD-29)


def _follow_up_id(args: dict[str, Any]) -> str:
    """Fail closed on a missing/garbage follow_up_id (same discipline as _ticket_id)."""
    follow_up_id = args.get("follow_up_id")
    if not isinstance(follow_up_id, str) or not follow_up_id.strip():
        raise ToolError("validation", "follow_up_id (string) is required")
    return follow_up_id


def _opt_iso(value: Any, *, field: str) -> str | None:
    """Validate an optional ISO datetime arg, kept as its string for the diff.

    Full value validation happens at apply time in the tracker (one validator);
    this is the propose-time fail-closed check that the string is even a date."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("validation", f"{field} must be an ISO datetime string")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError("validation", f"{field} is not a valid ISO datetime: {value!r}") from exc
    return value


def _store_follow_up_proposal(
    session: Session,
    *,
    actor_id: str,
    ticket_id: str,
    diff: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope,
) -> dict[str, Any]:
    """Store a pending ``agent_proposal`` for a follow-up write (propose-first).

    Identical contract to ``agent_action_propose``: the proposal row, its
    detailed ``proposal.create`` audit row, and its ``agent_proposals`` sync
    event commit atomically, so the proposal reaches every member's Inbox while
    the follow_up itself waits for a human approval (E08). The follow_up is
    written only when the approver decides it (``proposals.approve_proposal``).
    """
    ts = now()
    proposal = AgentProposal(
        ticket_id=ticket_id,
        proposer_id=actor_id,
        diff=diff,
        status="pending",
        created_at=ts,
        updated_at=ts,
    )
    session.add(proposal)
    session.flush()
    audit.write(
        session,
        actor_id=actor_id,
        action="proposal.create",
        source="mcp",
        object_ref=f"agent_proposals/{proposal.id}",
        after=audit.snapshot(proposal),
        now=ts,
    )
    EventLogSink(session, actor_id, signer=scope.signer).emit(
        DomainEvent(
            collection="agent_proposals",
            entity_id=proposal.id,
            op="patch",
            payload=audit.snapshot(proposal),
        )
    )
    session.commit()
    session.refresh(proposal)
    return {
        "proposal": {
            "id": proposal.id,
            "ticket_id": proposal.ticket_id,
            "proposer_id": proposal.proposer_id,
            "status": proposal.status,
            "diff": proposal.diff,
            "created_at": proposal.created_at.isoformat(),
        },
        "applied": False,
    }


def follow_up_create(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Propose a self-scheduled follow-up on a ticket (propose-first, E08).

    Stores a pending ``agent_proposal`` for the Inbox; the follow_up row is NOT
    written until a human approves it. The proposal's ``ticket_id`` is the
    follow_up's anchor ticket, so the Inbox shows which work it is about.
    """
    ticket_id = _ticket_id(args)
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("validation", "title (non-empty string) is required")
    body = args.get("body", "")
    if not isinstance(body, str):
        raise ToolError("validation", "body must be a string")
    due_at = _opt_iso(args.get("due_at"), field="due_at")

    # Fail before storing a proposal that anchors to a missing ticket.
    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        service.get_ticket(ticket_id)
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc

    follow_up: dict[str, Any] = {
        "title": title.strip(),
        "body": body,
        # provenance records the agent proposer, surviving the human approval.
        "provenance": {
            "origin": "agent",
            "actor_id": actor_id,
            "captured_at": now().isoformat(),
        },
    }
    if due_at is not None:
        follow_up["due_at"] = due_at
    diff = {"kind": "follow_up.create", "follow_up": follow_up}
    return _store_follow_up_proposal(
        session, actor_id=actor_id, ticket_id=ticket_id, diff=diff, now=now, scope=scope
    )


def follow_up_update(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Propose an edit to a follow-up's title/body/due_at (propose-first)."""
    follow_up_id = _follow_up_id(args)
    changes_raw = args.get("changes")
    if not isinstance(changes_raw, dict) or not changes_raw:
        raise ToolError("validation", "changes must be a non-empty object")
    allowed = {"title", "body", "due_at"}
    unknown = set(changes_raw) - allowed
    if unknown:
        raise ToolError(
            "validation", f"fields not updatable: {sorted(unknown)}; allowed: {sorted(allowed)}"
        )
    changes = dict(changes_raw)
    if "title" in changes and (
        not isinstance(changes["title"], str) or not changes["title"].strip()
    ):
        raise ToolError("validation", "title must be a non-empty string")
    if "body" in changes and not isinstance(changes["body"], str):
        raise ToolError("validation", "body must be a string")
    if "due_at" in changes:
        changes["due_at"] = _opt_iso(changes["due_at"], field="due_at")

    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        follow_up = service.get_follow_up(follow_up_id)
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc

    diff = {"kind": "follow_up.update", "follow_up_id": follow_up_id, "changes": changes}
    return _store_follow_up_proposal(
        session, actor_id=actor_id, ticket_id=follow_up.ticket_id, diff=diff, now=now, scope=scope
    )


def follow_up_complete(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Propose resolving a follow-up to done/dismissed (propose-first)."""
    follow_up_id = _follow_up_id(args)
    status = args.get("status", "done")
    if status not in ("done", "dismissed"):
        raise ToolError("validation", "status must be 'done' or 'dismissed'")

    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        follow_up = service.get_follow_up(follow_up_id)
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc

    diff = {"kind": "follow_up.complete", "follow_up_id": follow_up_id, "status": status}
    return _store_follow_up_proposal(
        session, actor_id=actor_id, ticket_id=follow_up.ticket_id, diff=diff, now=now, scope=scope
    )


def _follow_up_summary(follow_up: FollowUp) -> dict[str, Any]:
    """A fenced, light follow-up row for ``follow_up_search`` results."""
    return {
        "id": follow_up.id,
        "ticket_id": follow_up.ticket_id,
        "title": tag_untrusted(follow_up.title, "follow_up.title"),
        "body": tag_untrusted(follow_up.body, "follow_up.body"),
        "status": follow_up.status,
        "due_at": _iso(follow_up.due_at),
        "created_by": follow_up.created_by,
        "created_at": follow_up.created_at.isoformat(),
        "updated_at": follow_up.updated_at.isoformat(),
    }


def follow_up_search(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Read follow-ups by ticket / due-before / status (read-only).

    Human-authored strings (title, body) come back fenced untrusted. The
    gateway's eight checks have already authorized ``tickets.read``.
    """

    def _opt(key: str) -> str | None:
        value = args.get(key)
        return value if isinstance(value, str) and value.strip() else None

    status = _opt("status")
    if status is not None and status not in FOLLOW_UP_STATUSES:
        raise ToolError(
            "validation", f"unknown status {status!r}; expected one of {FOLLOW_UP_STATUSES}"
        )
    due_before_str = _opt_iso(args.get("due_before"), field="due_before")
    due_before = datetime.fromisoformat(due_before_str) if due_before_str is not None else None

    service = TrackerService(session, actor_id=actor_id, source="mcp")
    try:
        rows = service.search_follow_ups(
            ticket_id=_opt("ticket_id"), due_before=due_before, status=status
        )
    except TrackerValidationError as exc:
        raise ToolError("validation", str(exc)) from exc

    return {"follow_ups": [_follow_up_summary(f) for f in rows], "count": len(rows)}


# ---------------------------------------------------------- v0.1 read tools


def _ticket_summary(ticket: Ticket) -> dict[str, Any]:
    """A light, fenced ticket row for list/search results (no body/attachments)."""
    return {
        "id": ticket.id,
        "project_id": ticket.project_id,
        "title": tag_untrusted(ticket.title, "ticket.title"),
        "status": ticket.status,
        "priority": ticket.priority,
        "labels": [tag_untrusted(label, "ticket.label") for label in ticket.labels],
        "assignee": (
            tag_untrusted(ticket.assignee, "ticket.assignee")
            if ticket.assignee is not None
            else None
        ),
        "lifecycle_stage": ticket.lifecycle_stage,
        "parent_id": ticket.parent_id,
        "updated_at": ticket.updated_at.isoformat(),
    }


def _project_out(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "workspace_id": project.workspace_id,
        "name": tag_untrusted(project.name, "project.name"),
        "goal": tag_untrusted(project.goal, "project.goal"),
        "scope": tag_untrusted(project.scope, "project.scope"),
        "owner": project.owner,
        "status": project.status,
        "target_date": _iso(project.target_date),
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _memory_summary(entry: MemoryEntry) -> dict[str, Any]:
    """A fenced memory row without its body (search/preview list shape)."""
    return {
        "id": entry.id,
        "title": tag_untrusted(entry.title, "memory.title"),
        "space": entry.space,
        "type": entry.type,
        "review_status": entry.review_status,
        "confidence": entry.confidence,
        "updated_at": entry.updated_at.isoformat(),
    }


def _memory_out(entry: MemoryEntry) -> dict[str, Any]:
    """The full fenced memory entry (get / context-bundle shape)."""
    return {
        **_memory_summary(entry),
        "body": tag_untrusted(entry.body, "memory.body"),
        "source": entry.source,
        "linked_entities": list(entry.linked_entities),
        "expires_at": _iso(entry.expires_at),
        "created_at": entry.created_at.isoformat(),
    }


def _gate_memory_read(entry: MemoryEntry, scope: ToolScope, now: datetime) -> None:
    """The memory-policy check on a single read (8-check check 6).

    An agent session filters by its role policy (a withheld entry denies, no
    existence leak); a role-less agent is denied (it must declare a role); a
    human reads unfiltered.
    """
    if scope.memory_policy is not None:
        decision = memory_policy.decide(scope.memory_policy, entry, now=now)
        if not decision.included:
            raise PolicyDenied(
                f"memory entry withheld by policy ({decision.reason})",
                entry_id=entry.id,
                reason=decision.reason,
            )
    elif scope.is_agent:
        raise PolicyDenied(
            "an agent session must declare a context role (mcp-agent-role) to read memory",
            entry_id=entry.id,
            reason="no_agent_role",
        )


def _effective_role(args: dict[str, Any], scope: ToolScope) -> str:
    """The role a context resolve runs under (8-check check 6 for role_context).

    An agent session resolves *only* its own context role — a request for any
    other role is a denial (escalation attempt), not a silent override. A human
    session names the role to preview; a role-less agent is denied.
    """
    requested = args.get("role")
    if scope.agent_role is not None:
        if requested is not None and requested != scope.agent_role:
            raise PolicyDenied(
                "an agent session may only resolve its own role context",
                entry_id="role_context",
                reason="role_mismatch",
            )
        return scope.agent_role
    if scope.is_agent:
        raise PolicyDenied(
            "an agent session must declare a context role (mcp-agent-role)",
            entry_id="role_context",
            reason="no_agent_role",
        )
    if not isinstance(requested, str) or not memory_policy.is_agent_role(requested):
        raise ToolError(
            "validation",
            f"role is required, one of {sorted(memory_policy.ROLE_SLUGS)}",
        )
    return requested


def workspace_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Read the workspace the runtime serves (v0.1 is single-workspace)."""
    from sqlmodel import col, select

    workspace = session.exec(
        select(Workspace).order_by(col(Workspace.created_at), col(Workspace.id))
    ).first()
    if workspace is None:
        raise ToolError("not_found", "no workspace exists yet")
    return {
        "workspace": {
            "id": workspace.id,
            "name": tag_untrusted(workspace.name, "workspace.name"),
            "created_at": workspace.created_at.isoformat(),
            "updated_at": workspace.updated_at.isoformat(),
        }
    }


def project_list(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """List projects, newest first; optionally scoped to one workspace."""
    service = TrackerService(session, actor_id=actor_id, source="mcp", now=now)
    workspace_id = args.get("workspace_id")
    projects = service.list_projects(
        workspace_id=workspace_id if isinstance(workspace_id, str) else None
    )
    return {"projects": [_project_out(project) for project in projects]}


def project_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Read one project by id."""
    project_id = args.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ToolError("validation", "project_id (string) is required")
    service = TrackerService(session, actor_id=actor_id, source="mcp", now=now)
    try:
        project = service.get_project(project_id)
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc
    return {"project": _project_out(project)}


def ticket_search(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Search tickets by structured filters + an optional title/description term."""

    def _opt(key: str) -> str | None:
        value = args.get(key)
        return value if isinstance(value, str) and value else None

    service = TrackerService(session, actor_id=actor_id, source="mcp", now=now)
    tickets = service.list_tickets(
        project_id=_opt("project_id"),
        status=_opt("status"),
        assignee=_opt("assignee"),
        label=_opt("label"),
        stage=_opt("stage"),
        parent=_opt("parent"),
    )
    term = _opt("q")
    if term is not None:
        needle = term.lower()
        tickets = [
            t for t in tickets if needle in t.title.lower() or needle in t.description.lower()
        ]
    return {"tickets": [_ticket_summary(ticket) for ticket in tickets]}


def memory_search(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Search memory entries; an agent session sees only what its policy admits.

    The policy filtering is the service's ``search`` (the one enforced read
    path, MOD-19/MOD-21): the gateway only selects the session's policy and
    fences a role-less agent — it does not re-implement the filter, so the
    "never returns an excluded entry" guarantee lives in one place.
    """

    def _opt(key: str) -> str | None:
        value = args.get(key)
        return value if isinstance(value, str) and value else None

    if scope.memory_policy is None and scope.is_agent:
        raise PolicyDenied(
            "an agent session must declare a context role (mcp-agent-role) to read memory",
            entry_id="memory_search",
            reason="no_agent_role",
        )
    service = MemoryService(session, actor_id=actor_id, source="mcp", now=now)
    result = service.search(
        policy=scope.memory_policy, space=_opt("space"), type=_opt("type"), q=_opt("q")
    )
    return {"entries": [_memory_summary(entry) for entry in result.included]}


def memory_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Read one memory entry; the session's memory policy gates it (check 6)."""
    memory_id = args.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ToolError("validation", "memory_id (string) is required")
    service = MemoryService(session, actor_id=actor_id, source="mcp", now=now)
    try:
        entry = service.get_entry(memory_id)
    except MemoryNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc
    _gate_memory_read(entry, scope, now())
    return {"entry": _memory_out(entry)}


def memory_promote(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Propose a memory entry into the shared collection (the PROPOSE step).

    The agent-reachable half of promotion (``memory.write``, ``propose`` verb);
    approve/reject is human-only and deliberately has **no** MCP surface, so an
    agent can propose but never approve (the propose-first guard). This is the
    one memory **write** an agent performs, and it runs **only** through the
    gateway — the HTTP memory API is human/web-only — so every promotion is
    audited and passes the 8 checks.

    Promoting a ``local`` entry copies its content into a NEW ``team``
    ``proposed`` row and leaves the original ``local`` row immutable + unsynced
    (NFR-E13-1: no byte/id of the local entry leaves the machine); a ``team``
    ``{draft,stale}`` row transitions in place. The new/flipped row emits a
    **signed** event past the cutover (``scope.signer``), so it lands in every
    member's Inbox and is shared only once a human approves it.

    **Authorization (8-check #6 parity with the reads).** An agent may promote
    only what its role policy admits — the same gate ``memory_get`` enforces — so
    it cannot read-and-Inbox-inject an entry it is scoped out of (``promote``
    flips a ``team`` ``{draft,stale}`` row in place and this tool returns its
    ``body``; a ``role_context_preview`` discloses excluded *team* ids, so the id
    is reachable). The one gate deliberately **not** applied is the privacy
    (``local``) floor: the device owner's agent proposes the owner's own
    ``local`` note (the headline use case the floor would forbid), and a
    ``local`` id is never discoverable cross-scope (the read tools filter it
    out). A role-less agent is denied either way — declare a context role to
    touch memory.
    """
    memory_id = args.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ToolError("validation", "memory_id (string) is required")
    sink = EventLogSink(session, actor_id, signer=scope.signer)
    service = MemoryService(session, actor_id=actor_id, source="mcp", sink=sink, now=now)
    try:
        entry = service.get_entry(memory_id)
    except MemoryNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc
    if entry.visibility == "team":
        # Full read gate: denies a role-less agent and a policy-excluded team
        # entry (scope/status/expiry); team rows always clear the privacy floor.
        _gate_memory_read(entry, scope, now())
    elif scope.memory_policy is None and scope.is_agent:
        # A local row skips the privacy floor (it would wrongly deny), but a
        # role-less agent still cannot touch memory.
        raise PolicyDenied(
            "an agent session must declare a context role (mcp-agent-role) to promote memory",
            entry_id=entry.id,
            reason="no_agent_role",
        )
    try:
        promoted = service.promote(memory_id)
    except MemoryValidationError as exc:
        raise ToolError("validation", str(exc)) from exc
    return {"entry": _memory_out(promoted)}


def role_context_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Resolve the role-aware context bundle for a ticket (MOD-21 resolver)."""
    bundle = _resolve_bundle(session, args, actor_id=actor_id, now=now, scope=scope)
    return {
        "bundle": {
            "ticket_id": args["ticket_id"],
            "role": bundle.role,
            "policy_id": bundle.policy_id,
            # The live resolver gathers MemoryEntry rows (the bundle's protocol
            # type is the wider MemoryReadable the eval fixtures also satisfy).
            "included": [_memory_out(cast("MemoryEntry", entry)) for entry in bundle.included],
            "token_estimate": bundle.token_estimate,
        }
    }


def role_context_preview(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Preview a bundle: included, excluded-with-reason, missing, token estimate."""
    bundle = _resolve_bundle(session, args, actor_id=actor_id, now=now, scope=scope)
    return {
        "bundle": {
            "ticket_id": args["ticket_id"],
            "role": bundle.role,
            "policy_id": bundle.policy_id,
            "rationale": bundle.rationale,
            "included": [_memory_summary(cast("MemoryEntry", entry)) for entry in bundle.included],
            "excluded": [
                {"memory_id": item.entry_id, "reason": item.reason} for item in bundle.excluded
            ],
            "missing": list(bundle.missing),
            "token_estimate": bundle.token_estimate,
        }
    }


def _resolve_bundle(
    session: Session,
    args: dict[str, Any],
    *,
    actor_id: str,
    now: Callable[[], datetime],
    scope: ToolScope,
) -> context.ContextBundle:
    ticket_id = args.get("ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        raise ToolError("validation", "ticket_id (string) is required")
    role = _effective_role(args, scope)
    service = TrackerService(session, actor_id=actor_id, source="mcp", now=now)
    try:
        ticket = service.get_ticket(ticket_id)
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc
    return context.resolve_for_ticket(session, ticket, role, actor_id=actor_id, now=now())


# --------------------------------------------------------- v0.1 write tools


def ticket_comment_create(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Append a comment to a ticket — the agent's communication channel.

    A comment mutates no tracked field (propose-first is unaffected); it is
    fully attributed + audited + synced by the tracker's one write path. The
    body is fenced on the way back: it may carry quoted untrusted material.
    """
    body = args.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ToolError("validation", "body (non-empty string) is required")
    # The comment is a syncable write: give the service a sink (signed past the
    # cutover) so the comment reaches every member's replica.
    sink = EventLogSink(session, actor_id, signer=scope.signer)
    service = TrackerService(session, actor_id=actor_id, source="mcp", sink=sink, now=now)
    try:
        comment = service.add_comment(_ticket_id(args), body)
    except TrackerNotFoundError as exc:
        raise ToolError("not_found", str(exc)) from exc
    except TrackerValidationError as exc:
        raise ToolError("validation", str(exc)) from exc
    return {
        "comment": {
            "id": comment.id,
            "ticket_id": comment.ticket_id,
            "author_actor_id": comment.author_actor_id,
            "body": tag_untrusted(comment.body, "comment.body"),
            "created_at": comment.created_at.isoformat(),
        }
    }


def agent_action_approve(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
    scope: ToolScope = UNSCOPED,
) -> dict[str, Any]:
    """Approve a pending proposal — apply its diff through the one apply path.

    A human/approver verb (requires ``tickets.write``; an agent's
    ``proposals.write`` can never reach it). Delegates to
    ``kantaq_core.proposals.approve_proposal`` so the MCP approve and the Inbox
    approve share exactly one validated, audited apply.
    """
    proposal_id = args.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise ToolError("validation", "proposal_id (string) is required")
    try:
        proposal, ticket = proposals.approve_proposal(
            session, proposal_id, actor_id=actor_id, source="mcp", now=now, signer=scope.signer
        )
    except proposals.ProposalError as exc:
        raise ToolError(exc.code, exc.message) from exc
    return {
        "proposal": {
            "id": proposal.id,
            "ticket_id": proposal.ticket_id,
            "status": proposal.status,
        },
        "ticket": {
            "id": ticket.id,
            "status": ticket.status,
            "lifecycle_stage": ticket.lifecycle_stage,
            "updated_at": ticket.updated_at.isoformat(),
        },
        "applied": True,
    }
