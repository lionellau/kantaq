"""Agent-proposal decisions: the one approve / reject path (MOD-12 + MOD-09).

A proposal is created only through the MCP gateway (``agent_action_propose``)
and decided by an approver — either a human in the Inbox (the runtime
``/v1/proposals`` API, E20) or via the ``agent_action_approve`` MCP tool (E10).
Both decide through **this** module so there is exactly one apply path (the
codebase's "one validator, no drift" rule):

* **Approve** applies the proposal's diff to the ticket through
  ``TrackerService.update_ticket`` (validate → apply → audit → emit), so full
  value validation happens at apply time exactly like a human edit. The
  proposal's status flip and the ticket patch share one transaction — the flip
  is staged first and ``TrackerService`` commits both, so a validation failure
  leaves the proposal pending and the ticket untouched.
* The status flip is a **compare-and-swap** (a conditional UPDATE re-checking
  ``status = 'pending'`` under the write lock): two concurrent decisions cannot
  both apply — the loser matches zero rows and raises ``ProposalConflictError``.

Audit then shows two distinct actors — the proposer on ``proposal.create``
(written at propose time) and the approver on ``proposal.approve`` +
``ticket.update`` — the dogfood-gate criterion. ``source`` distinguishes the
surface (``app`` for the Inbox, ``mcp`` for the tool).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col

from kantaq_core import audit
from kantaq_core.telemetry import TelemetryService
from kantaq_core.tracker.events import DomainEvent
from kantaq_core.tracker.service import (
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
)
from kantaq_db.models import AgentProposal, Ticket
from kantaq_sync_engine.log import (
    EventLogSink,
    EventSigner,
    mark_proposal_origin,
    next_actor_seq,
)

# ``rebase_required`` (MOD-26 §B3 / E05-T3): an approved proposal whose ticket
# write was found stale-and-contending at the sync commit point — the team moved
# the field past the base the agent proposed against. The proposal is bounced
# back to the human to re-decide against current reality; it is NOT applied and
# the intervening commit is never clobbered (the §8.5 propose-first rule).
PROPOSAL_STATUSES = ("pending", "approved", "rejected", "rebase_required")

# Diff fields whose JSON value is an ISO string the tracker expects as a
# datetime; the rest of the patchable fields are already JSON-native and the
# tracker validates them at apply time (one validator).
_DATETIME_FIELDS = ("due_date",)


class ProposalError(Exception):
    """A proposal decision failed; nothing is applied. Carries a structured code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProposalNotFoundError(ProposalError):
    def __init__(self, proposal_id: str) -> None:
        super().__init__("not_found", f"no such proposal: {proposal_id}")


class ProposalConflictError(ProposalError):
    def __init__(self, message: str) -> None:
        super().__init__("conflict", message)


class ProposalChangesError(ProposalError):
    def __init__(self, message: str) -> None:
        super().__init__("validation", message)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _get_pending(session: Session, proposal_id: str) -> AgentProposal:
    proposal = session.get(AgentProposal, proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(proposal_id)
    if proposal.status != "pending":
        raise ProposalConflictError(f"proposal is already {proposal.status}, not pending")
    return proposal


def coerce_changes(diff: Any) -> dict[str, Any]:
    """The applicable, type-coerced change set from a proposal diff.

    Datetime fields are parsed from their ISO strings; every other value — and
    any unknown field (a silently-dropped key would be a silent bypass, SEC
    review) — is validated by the tracker's one validator at apply time.
    """
    changes_raw = diff.get("changes") if isinstance(diff, dict) else None
    if not isinstance(changes_raw, dict) or not changes_raw:
        raise ProposalChangesError("proposal carries no applicable changes")
    # Unknown fields are rejected by ``TrackerService.update_ticket`` at apply
    # time (the one validator) — a dropped key can never become a silent bypass.
    changes = dict(changes_raw)
    for field in _DATETIME_FIELDS:
        value = changes.get(field)
        if isinstance(value, str):
            try:
                changes[field] = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ProposalChangesError(
                    f"{field} is not a valid ISO datetime: {value!r}"
                ) from exc
    return changes


def _flip_status(
    session: Session,
    proposal: AgentProposal,
    *,
    actor_id: str,
    source: str,
    status: str,
    now: datetime,
    signer: EventSigner | None = None,
    reason: str | None = None,
) -> None:
    """Stage the compare-and-swap status flip with its audit row and sync event.

    ``signer`` (E04-T4) signs the emitted ``agent_proposals`` event past the
    signing cutover; ``None`` leaves it unsigned (pre-cutover / solo).

    ``reason`` (E20-T6) is the human's optional note when declining a proposal;
    it rides the audit row's ``after`` (the same lift-from-``after`` convention
    the gateway uses for a denial's reason), so it reaches the proposing agent's
    owner through the audit trail. It is *not* added to the emitted
    ``agent_proposals`` payload — the synced entity has no reason column."""
    before = audit.snapshot(proposal)
    claimed = cast(
        "CursorResult[Any]",
        session.execute(
            sa_update(AgentProposal)
            .where(col(AgentProposal.id) == proposal.id, col(AgentProposal.status) == "pending")
            .values(status=status, updated_at=now)
        ),
    )
    if claimed.rowcount != 1:
        session.rollback()
        raise ProposalConflictError("proposal was decided concurrently")
    session.refresh(proposal)
    after = audit.snapshot(proposal)
    audit.write(
        session,
        actor_id=actor_id,
        action=f"proposal.{'approve' if status == 'approved' else 'reject'}",
        source=source,
        object_ref=f"agent_proposals/{proposal.id}",
        before=before,
        # The reason lifts out of ``after`` (AuditEventOut), never widening the
        # synced proposal entity below.
        after={**after, "reason": reason} if reason else after,
        now=now,
    )
    # The decision syncs to every replica (FR-E20-1: one queue, synced), signed
    # at append when the runtime is post-cutover (E04-T4). The payload is the
    # bare entity snapshot — the reason is an audit detail, not an entity field.
    EventLogSink(session, actor_id, signer=signer).emit(
        DomainEvent(
            collection="agent_proposals",
            entity_id=proposal.id,
            op="patch",
            payload=after,
        )
    )
    # Telemetry (E28, opt-in no-op): outcome + wait — numbers only, no content.
    TelemetryService(session, now=lambda: now).record(
        f"proposal_{status}",
        {"seconds_to_decision": (now - proposal.created_at).total_seconds()},
    )


def approve_proposal(
    session: Session,
    proposal_id: str,
    *,
    actor_id: str,
    source: str,
    now: Callable[[], datetime] | None = None,
    signer: EventSigner | None = None,
) -> tuple[AgentProposal, Ticket]:
    """Apply a pending proposal's diff to its ticket; returns (proposal, ticket).

    Raises ``ProposalError`` (``not_found`` / ``conflict`` / ``validation``) on
    any failure, having applied nothing. ``signer`` (E04-T4) signs both the
    ``agent_proposals`` decision event and the ``tickets`` patch event past the
    cutover.
    """
    ts = (now or _now)()
    proposal = _get_pending(session, proposal_id)
    changes = coerce_changes(proposal.diff)
    _flip_status(
        session,
        proposal,
        actor_id=actor_id,
        source=source,
        status="approved",
        now=ts,
        signer=signer,
    )
    sink = EventLogSink(session, actor_id, signer=signer)
    service = TrackerService(session, actor_id=actor_id, source=source, sink=sink, now=lambda: ts)
    # The actor_seq the ticket patch will take — captured before the emit so we
    # can tag exactly that event as proposal-originated (MOD-26 §B3 / E05-T3),
    # without threading a proposal id through the tracker domain.
    patch_seq = next_actor_seq(session, actor_id)
    try:
        ticket = service.update_ticket(proposal.ticket_id, changes)
    except TrackerNotFoundError as exc:
        session.rollback()
        raise ProposalNotFoundError(proposal.ticket_id) from exc
    except TrackerValidationError as exc:
        session.rollback()
        raise ProposalChangesError(str(exc)) from exc
    # Tag the just-emitted ticket write so a stale-and-contending sync commit can
    # bounce this proposal to rebase_required instead of minting a conflict_record.
    mark_proposal_origin(session, actor_id, patch_seq, proposal.id)
    session.refresh(proposal)
    return proposal, ticket


def reject_proposal(
    session: Session,
    proposal_id: str,
    *,
    actor_id: str,
    source: str,
    now: Callable[[], datetime] | None = None,
    signer: EventSigner | None = None,
    reason: str | None = None,
) -> AgentProposal:
    """Decline a pending proposal; the ticket is never touched.

    ``reason`` (E20-T6) is the human's optional "why", carried in the audit row
    so the proposing agent's owner can see it."""
    ts = (now or _now)()
    proposal = _get_pending(session, proposal_id)
    _flip_status(
        session,
        proposal,
        actor_id=actor_id,
        source=source,
        status="rejected",
        now=ts,
        signer=signer,
        reason=reason,
    )
    session.commit()
    session.refresh(proposal)
    return proposal


__all__ = [
    "PROPOSAL_STATUSES",
    "ProposalChangesError",
    "ProposalConflictError",
    "ProposalError",
    "ProposalNotFoundError",
    "approve_proposal",
    "coerce_changes",
    "reject_proposal",
]
