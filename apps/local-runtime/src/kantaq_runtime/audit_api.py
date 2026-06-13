"""Audit API: the live audit-log read for the trust surfaces (E20-T3, MOD-12, SEC).

``GET /v1/audit/range`` is the one read over the append-only audit log (MOD-07).
It feeds the Agents page's *recent + denied calls* and the Inbox's *denied
calls* tab, both of which must reflect **live** audit with no stale cache
(NFR-E20-1): every poll re-reads the log, so a denial shows up the instant the
gateway writes it.

Curated by design (SEC): a row's ``before``/``after`` can hold a full entity
snapshot (a ticket, a proposal), so this endpoint never echoes them raw. It
returns the attribution (actor, action, object, source, time) plus the three
fields the gateway records on a denial — ``reason``, ``detail``, ``session_id``
— lifted out of ``after``. Denied calls are ``action == "tool.deny"``,
``source == "mcp"``.

SEC boundary (mirrors grants/agents, E27 review): a caller reads their own trail
by default; reading another member's, or the whole workspace's, needs
``tokens.rotate``. An explicit ``actor`` that is not the caller's, without that
permission, is a 403 — the audit log is not cross-member readable by default.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.identity import Action, VerifiedActor, can
from kantaq_db.models import AuditEvent
from kantaq_runtime.auth import get_engine_dep, require_actor

router = APIRouter(prefix="/v1/audit", tags=["audit"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]

# A range read is for "recent" activity, not a log export — cap it so a single
# call cannot scan the whole table (the gateway writes one denial per blocked
# call; 200 covers a long agent session's worth of triage).
_MAX_LIMIT = 200


def _str_field(after: dict[str, Any] | None, key: str) -> str | None:
    """A string sub-field of an audit row's ``after``, or None — never a snapshot."""
    if isinstance(after, dict):
        value = after.get(key)
        if isinstance(value, str):
            return value
    return None


class AuditEventOut(BaseModel):
    id: str
    actor_id: str
    action: str
    object_ref: str | None
    source: str
    created_at: datetime
    # Lifted out of ``after`` for a denial (``tool.deny``); None for other rows.
    reason: str | None
    detail: str | None
    session_id: str | None

    @classmethod
    def from_row(cls, row: AuditEvent) -> AuditEventOut:
        return cls(
            id=row.id,
            actor_id=row.actor_id,
            action=row.action,
            object_ref=row.object_ref,
            source=row.source,
            created_at=row.created_at,
            reason=_str_field(row.after, "reason"),
            detail=_str_field(row.after, "detail"),
            session_id=_str_field(row.after, "session_id"),
        )


@router.get("/range", response_model=list[AuditEventOut])
def audit_range(
    actor: AnyActor,
    engine: EngineDep,
    request: Request,
    member: str | None = None,
    action: str | None = None,
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 50,
) -> list[AuditEventOut]:
    """Most-recent-first audit rows, live off the append-only log (no cache).

    ``member`` scopes to one actor's trail (defaults to the caller; the whole
    workspace only for ``tokens.rotate`` holders). ``action="tool.deny"`` +
    ``source="mcp"`` is the denied-calls view. ``limit`` is capped at 200.
    """
    full = can(actor.role, Action.tokens_rotate, scopes=list(actor.scopes))
    if member is None:
        target = None if full else actor.member_id
    else:
        if member != actor.member_id and not full:
            raise HTTPException(
                status_code=403,
                detail="reading another member's audit trail needs tokens.rotate",
            )
        target = member
    with Session(engine) as session:
        rows = audit.read_range(session, actor_id=target, action=action, source=source, limit=limit)
        return [AuditEventOut.from_row(row) for row in rows]
