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
from datetime import datetime
from typing import Any

from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.tracker.events import DomainEvent
from kantaq_core.tracker.service import (
    TICKET_PRIORITIES,
    TICKET_STATUSES,
    TrackerNotFoundError,
    TrackerService,
)
from kantaq_db.models import AgentProposal
from kantaq_mcp.security import tag_untrusted
from kantaq_sync_engine.log import EventLogSink

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


class ToolError(Exception):
    """A domain-level tool failure, returned to the agent as a structured error.

    Not a gateway denial: the call was permitted, the domain said no (unknown
    ticket, invalid proposal). Nothing is persisted when this raises.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def ticket_get(
    session: Session,
    *,
    actor_id: str,
    args: dict[str, Any],
    now: Callable[[], datetime],
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
    EventLogSink(session, actor_id).emit(
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
