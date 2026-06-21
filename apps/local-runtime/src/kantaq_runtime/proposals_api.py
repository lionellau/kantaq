"""Proposals API: list, approve, reject (E20, MOD-12's server half).

The Inbox reads and decides agent proposals here. A proposal is created only
through the MCP gateway (``agent_action_propose``, MOD-09) and syncs like any
collection, so this surface never creates one — it lists the local replica's
rows and lets a human commit or decline the proposed change.

Approve and reject both route through the one apply path
(``kantaq_core.proposals``): the compare-and-swap status flip + (for approve)
the diff applied through ``TrackerService``, in one transaction. So the MCP
``agent_action_approve`` tool and this human Inbox share exactly one validated,
audited, CAS-guarded path (no drift). Audit shows two distinct actors — the
proposer on ``proposal.create`` (propose time) and the approver on
``proposal.approve`` + ``ticket.update`` — the dogfood-gate criterion.

Permissions: reading the queue needs ``tickets.read``; approve and reject need
``tickets.write`` (a decision is a ticket write). Agent scopes carry only
``proposals.write``, so an agent can never approve its own proposal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq_core import proposals
from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.notifications import NotificationEvent
from kantaq_core.telemetry import TelemetryService
from kantaq_core.tracker import TrackerService
from kantaq_db.models import AgentProposal, Ticket
from kantaq_runtime.auth import (
    get_engine_dep,
    get_event_signer,
    require_action,
    require_human_action,
)
from kantaq_runtime.notifications import notify_in_background
from kantaq_runtime.tracker_api import TicketOut
from kantaq_sync_engine import EventSigner

router = APIRouter(prefix="/v1/proposals", tags=["proposals"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]
# Approve/reject are a human decision (a ticket write) — and human-only: an agent
# proposes through the gateway but can never decide its own proposal, even with an
# over-scoped token (DEBT-37 / D-33, the boundary half of the issuance clamp).
WriterActor = Annotated[VerifiedActor, Depends(require_human_action(Action.tickets_write))]
# The "notify the approver" nudge (E20-T9 / §16.10) is a human-only read action: a
# teammate flags a pending proposal as needing a decision. An agent proposes but
# never nudges — it has no business triggering outbound traffic (the dispatch
# stays a human-initiated, content-free signal).
NudgerActor = Annotated[VerifiedActor, Depends(require_human_action(Action.tickets_read))]
# A decision (approve/reject) is a ticket write, so it carries the device signer
# (E04-T4): the agent_proposals + tickets events are signed by the approver past
# the cutover, the same as any other runtime write.
SignerDep = Annotated[EventSigner | None, Depends(get_event_signer)]

PROPOSAL_STATUSES = proposals.PROPOSAL_STATUSES

# A proposal decision is a ticket write through the one apply path
# (``kantaq_core.proposals``); this surface maps its structured errors to HTTP.
_ERROR_STATUS = {"not_found": 404, "conflict": 409, "validation": 422}


def _http(exc: proposals.ProposalError) -> HTTPException:
    return HTTPException(status_code=_ERROR_STATUS[exc.code], detail=exc.message)


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


class RejectIn(BaseModel):
    """An optional human reason when declining a proposal (E20-T6). The reason
    rides the audit trail and reaches the proposing agent's owner; it never
    touches the ticket."""

    reason: str | None = Field(default=None, max_length=2000)


def _ticket_title(session: Session, ticket_id: str) -> str | None:
    ticket = session.get(Ticket, ticket_id)
    return ticket.title if ticket is not None else None


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
        if TelemetryService(session).record("proposals_listed", {"count": len(rows)}):
            session.commit()
        return [
            ProposalOut.from_row(row, ticket_title=_ticket_title(session, row.ticket_id))
            for row in rows
        ]


def _notify(
    background_tasks: BackgroundTasks, request: Request, engine: Engine, event: NotificationEvent
) -> None:
    """Queue a content-free notification to fire AFTER the response (E20-T8).

    Post-response + post-commit, so a slow/dead sink never blocks the decision.
    A test may inject ``app.state.notification_client_factory`` to capture the
    POST; production leaves it unset (the dispatcher uses a real httpx client).
    """
    factory = getattr(request.app.state, "notification_client_factory", None)
    background_tasks.add_task(notify_in_background, engine, event, client_factory=factory)


@router.post("/{proposal_id}/approve", response_model=ApproveOut)
def approve_proposal(
    proposal_id: str,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
    background_tasks: BackgroundTasks,
    request: Request,
) -> ApproveOut:
    with Session(engine) as session:
        try:
            # The one apply path (kantaq_core.proposals): compare-and-swap flip,
            # then the diff through TrackerService — both in one transaction.
            proposal, ticket = proposals.approve_proposal(
                session, proposal_id, actor_id=actor.member_id, source="app", signer=signer
            )
        except proposals.ProposalError as exc:
            raise _http(exc) from exc
        service = TrackerService(session, actor_id=actor.member_id, source="app")
        result = ApproveOut(
            proposal=ProposalOut.from_row(proposal, ticket_title=ticket.title),
            ticket=TicketOut.from_row(
                ticket, recommended_next_stages=service.recommended_next(ticket)
            ),
        )
        proposal_ref, ticket_ref = proposal.id, ticket.id
    _notify(
        background_tasks,
        request,
        engine,
        NotificationEvent(
            action="proposal.approved",
            ids=(proposal_ref, ticket_ref),
            actor_id=actor.member_id,
            deep_link=f"/tickets/{ticket_ref}",
        ),
    )
    return result


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(
    proposal_id: str,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
    background_tasks: BackgroundTasks,
    request: Request,
    body: RejectIn | None = None,
) -> ProposalOut:
    # An empty or whitespace-only reason is "no reason" — keep the audit clean.
    raw = body.reason if body is not None else None
    reason = raw.strip() if raw and raw.strip() else None
    with Session(engine) as session:
        try:
            proposal = proposals.reject_proposal(
                session,
                proposal_id,
                actor_id=actor.member_id,
                source="app",
                signer=signer,
                reason=reason,
            )
        except proposals.ProposalError as exc:
            raise _http(exc) from exc
        result = ProposalOut.from_row(
            proposal, ticket_title=_ticket_title(session, proposal.ticket_id)
        )
        proposal_ref, ticket_ref = proposal.id, proposal.ticket_id
    # The reason rides the audit trail (E20-T6), NOT the notification — the
    # signal stays content-free (ids + action + actor + deep-link only).
    _notify(
        background_tasks,
        request,
        engine,
        NotificationEvent(
            action="proposal.rejected",
            ids=(proposal_ref, ticket_ref),
            actor_id=actor.member_id,
            deep_link=f"/tickets/{ticket_ref}",
        ),
    )
    return result


@router.post("/{proposal_id}/notify", status_code=204)
def notify_approver(
    proposal_id: str,
    actor: NudgerActor,
    engine: EngineDep,
    background_tasks: BackgroundTasks,
    request: Request,
) -> Response:
    """Nudge the approver that a still-pending proposal needs a decision (E20-T9).

    Fires the same content-free signal (``proposal.pending``) to the configured
    sink — opt-in, no-op when notifications are off. Only a still-pending
    proposal can be nudged (a decided one needs no decision).
    """
    with Session(engine) as session:
        proposal = session.get(AgentProposal, proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail=f"no such proposal: {proposal_id}")
        if proposal.status != "pending":
            raise HTTPException(
                status_code=409, detail=f"proposal is already {proposal.status}, not pending"
            )
        ticket_ref = proposal.ticket_id
    _notify(
        background_tasks,
        request,
        engine,
        NotificationEvent(
            action="proposal.pending",
            ids=(proposal_id, ticket_ref),
            actor_id=actor.member_id,
            deep_link=f"/tickets/{ticket_ref}",
        ),
    )
    return Response(status_code=204)
