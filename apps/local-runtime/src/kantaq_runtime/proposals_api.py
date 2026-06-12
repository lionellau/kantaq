"""Proposals API: list, approve, reject (E20, MOD-12's server half).

The Inbox reads and decides agent proposals here. A proposal is created only
through the MCP gateway (``agent_action_propose``, MOD-09) and syncs like any
collection, so this surface never creates one — it lists the local replica's
rows and lets a human commit or decline the proposed change.

Approve applies the proposal's diff to the ticket **through TrackerService**
(the one write path: validate → apply → audit → emit), so full value
validation happens at apply time exactly like a human edit. The proposal's
status flip and the ticket patch share one transaction: the status flip is
staged first and ``TrackerService`` commits both, so a validation failure
leaves the proposal pending and the ticket untouched. Audit then shows two
distinct actors — the proposer on ``proposal.create`` (written at propose
time) and the approver on ``proposal.approve`` + ``ticket.update`` (written
here) — which is the sprint's dogfood-gate criterion #4.

Permissions: reading the queue needs ``tickets.read``; approve and reject need
``tickets.write`` (a decision is a ticket write). Agent scopes carry only
``proposals.write``, so an agent can never approve its own proposal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult, Engine
from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.tracker import TrackerNotFoundError, TrackerService, TrackerValidationError
from kantaq_core.tracker.events import DomainEvent
from kantaq_db.models import AgentProposal, Ticket
from kantaq_runtime.auth import get_engine_dep, require_action
from kantaq_runtime.tracker_api import TicketOut, TicketPatch, _domain
from kantaq_sync_engine import EventLogSink

router = APIRouter(prefix="/v1/proposals", tags=["proposals"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]
WriterActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_write))]

PROPOSAL_STATUSES = ("pending", "approved", "rejected")


def _now() -> datetime:
    # Naive UTC — the one timestamp encoding the store holds (the MOD-03 rule:
    # SQLite drops tzinfo, and events must fold back byte-identically).
    return datetime.now(UTC).replace(tzinfo=None)


class ProposalOut(BaseModel):
    id: str
    ticket_id: str
    ticket_title: str | None
    proposer_id: str
    status: str
    diff: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: AgentProposal, *, ticket_title: str | None) -> ProposalOut:
        return cls(
            id=row.id,
            ticket_id=row.ticket_id,
            ticket_title=ticket_title,
            proposer_id=row.proposer_id,
            status=row.status,
            diff=row.diff,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ApproveOut(BaseModel):
    proposal: ProposalOut
    ticket: TicketOut


def _get_pending(session: Session, proposal_id: str) -> AgentProposal:
    proposal = session.get(AgentProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no such proposal: {proposal_id}")
    if proposal.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"proposal is already {proposal.status}, not pending"
        )
    return proposal


def _ticket_title(session: Session, ticket_id: str) -> str | None:
    ticket = session.get(Ticket, ticket_id)
    return ticket.title if ticket is not None else None


def _flip_status(
    session: Session, proposal: AgentProposal, *, actor_id: str, status: str, ts: datetime
) -> None:
    """Stage the status flip with its audit row and sync event — no commit.

    The caller decides the transaction boundary: approve lets TrackerService
    commit (one transaction with the ticket patch); reject commits itself.

    The flip is a compare-and-swap: handlers run in a threadpool, so two
    decisions can both read "pending" before either commits. The conditional
    UPDATE takes the write lock and re-checks the status under it — the loser
    matches zero rows and 409s instead of applying twice (SEC second review,
    must-fix).
    """
    before = audit.snapshot(proposal)
    claimed = cast(
        "CursorResult[Any]",
        session.execute(
            sa_update(AgentProposal)
            .where(col(AgentProposal.id) == proposal.id, col(AgentProposal.status) == "pending")
            .values(status=status, updated_at=ts)
        ),
    )
    if claimed.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="proposal was decided concurrently")
    session.refresh(proposal)
    audit.write(
        session,
        actor_id=actor_id,
        action=f"proposal.{'approve' if status == 'approved' else 'reject'}",
        source="app",
        object_ref=f"agent_proposals/{proposal.id}",
        before=before,
        after=audit.snapshot(proposal),
        now=ts,
    )
    # The decision syncs to every replica (FR-E20-1: one queue, synced).
    EventLogSink(session, actor_id).emit(
        DomainEvent(
            collection="agent_proposals",
            entity_id=proposal.id,
            op="patch",
            payload=audit.snapshot(proposal),
        )
    )


@router.get("", response_model=list[ProposalOut])
def list_proposals(
    actor: ReaderActor,
    engine: EngineDep,
    status: str | None = None,
    ticket: str | None = None,
) -> list[ProposalOut]:
    if status is not None and status not in PROPOSAL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown proposal status {status!r}; expected one of {PROPOSAL_STATUSES}",
        )
    with Session(engine) as session:
        statement = select(AgentProposal)
        if status is not None:
            statement = statement.where(AgentProposal.status == status)
        if ticket is not None:
            statement = statement.where(AgentProposal.ticket_id == ticket)
        rows = sorted(session.exec(statement).all(), key=lambda p: p.id, reverse=True)
        return [
            ProposalOut.from_row(row, ticket_title=_ticket_title(session, row.ticket_id))
            for row in rows
        ]


@router.post("/{proposal_id}/approve", response_model=ApproveOut)
def approve_proposal(proposal_id: str, actor: WriterActor, engine: EngineDep) -> ApproveOut:
    with Session(engine) as session:
        proposal = _get_pending(session, proposal_id)

        changes_raw = proposal.diff.get("changes") if isinstance(proposal.diff, dict) else None
        if not isinstance(changes_raw, dict) or not changes_raw:
            raise HTTPException(status_code=422, detail="proposal carries no applicable changes")
        try:
            # Coerce the JSON diff through the same shapes a human PATCH uses
            # (ISO strings become datetimes); value validation happens next,
            # inside TrackerService, at apply time.
            patch = TicketPatch.model_validate(changes_raw)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        changes = patch.model_dump(exclude_unset=True)

        ts = _now()
        _flip_status(session, proposal, actor_id=actor.member_id, status="approved", ts=ts)
        sink = EventLogSink(session, actor.member_id)
        service = TrackerService(session, actor_id=actor.member_id, source="app", sink=sink)
        try:
            # Commits the staged status flip and the ticket patch together.
            ticket = service.update_ticket(proposal.ticket_id, changes)
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            session.rollback()
            raise _domain(exc) from exc
        session.refresh(proposal)
        return ApproveOut(
            proposal=ProposalOut.from_row(proposal, ticket_title=ticket.title),
            ticket=TicketOut.from_row(ticket),
        )


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(proposal_id: str, actor: WriterActor, engine: EngineDep) -> ProposalOut:
    with Session(engine) as session:
        proposal = _get_pending(session, proposal_id)
        _flip_status(session, proposal, actor_id=actor.member_id, status="rejected", ts=_now())
        session.commit()
        session.refresh(proposal)
        return ProposalOut.from_row(
            proposal, ticket_title=_ticket_title(session, proposal.ticket_id)
        )
