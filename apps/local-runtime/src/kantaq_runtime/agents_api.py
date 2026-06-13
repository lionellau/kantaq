"""Agents API: the live agent-session trust surface (E20-T3, MOD-12, SEC).

The Agents page must be **honest and complete** (PRD §16.7, NFR-E20-1): every
agent session and every denied call is here, read live from the durable record
— never a cache. A v0.1 session is derived 1:1 from a capability grant (MOD-08
``derive_session_from_grant``). The gateway holds the *live* session in memory
in its own process, so the cross-process source of truth the web runtime can
read is the **signed grant** (the authority) plus the **audit log** (the
activity). This endpoint lists agent grants as sessions; ``/v1/audit/range``
(``audit_api``) supplies each one's recent + denied calls.

An "agent session" is a grant whose subject is an **Agent-role** member — the
v0.1 model gives every agent its own member (``role=Agent``). Human members hold
self-grants too (for event signing, ``ensure_member_grant``); those are not
agent sessions and are deliberately excluded so the page is not padded with
non-sessions.

SEC boundary (mirrors the grants surface, E27 review): enumerating the whole
workspace's agents needs ``tokens.rotate`` — cross-member grant data is
credential-management material; without it a caller sees only agent grants
subjected to themselves. This is a **read** surface: revoke and rotate reuse the
existing audited paths — ``POST /v1/grants/{id}/revoke`` (which kills the derived
session within the revocation budget, NFR-E06-2) and ``POST /v1/members/{id}/rotate``
— so the page never offers a control that does nothing (the MOD-12 honesty rule).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.identity import Action, GrantService, Role, VerifiedActor, can
from kantaq_db.models import CapabilityGrantRow, Member
from kantaq_runtime.auth import get_engine_dep, keychain_for, require_actor
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/agents", tags=["agents"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]

# A propose-capable grant runs in propose-only write mode; everything else is
# read-only (v0.1 has no direct_write, FR-E09-4). The enforced mapping lives in
# the gateway; this is the display-only echo of the one verb that flips it.
_PROPOSE_VERB = Action.proposals_write.value


class AgentSessionOut(BaseModel):
    grant_id: str
    owner_member_id: str
    owner_email: str | None
    owner_role: str | None
    resource: str
    verbs: list[str]
    write_mode: str
    issued_at: int
    expires_at: int
    revoked_at: str | None
    active: bool
    reason: str

    @classmethod
    def from_grant(
        cls, row: CapabilityGrantRow, *, service: GrantService, member: Member | None
    ) -> AgentSessionOut:
        verification = service.verify(row)
        return cls(
            grant_id=row.id,
            owner_member_id=row.subject,
            owner_email=member.email if member is not None else None,
            owner_role=member.role if member is not None else None,
            resource=row.resource,
            verbs=list(row.verbs),
            write_mode="propose_only" if _PROPOSE_VERB in row.verbs else "read_only",
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at.isoformat() if row.revoked_at is not None else None,
            active=verification.ok,
            reason=verification.reason,
        )


@router.get("/sessions", response_model=list[AgentSessionOut])
def list_sessions(actor: AnyActor, engine: EngineDep, request: Request) -> list[AgentSessionOut]:
    """Live agent sessions, derived from the workspace's capability grants.

    ``tokens.rotate`` holders see every agent; everyone else sees only grants
    subjected to themselves. Newest grant first. No cache: a revoked grant flips
    ``active`` to false on the next poll, the same instant the revocation commits.

    Completeness over neatness (NFR-E20-1 — "every agent session is here"): a
    grant is shown when its subject is an Agent member, **or** its subject has
    any gateway activity (it is being used as a session even if its role isn't
    Agent), **or** its subject member row is missing (an anomaly the overseer
    must see, never a silent drop). Only a pure human signing self-grant that was
    never used as a session is omitted — it is not a session.
    """
    settings: Settings = request.app.state.settings
    full = can(actor.role, Action.tokens_rotate, scopes=list(actor.scopes))
    with Session(engine) as session:
        service = GrantService(session, keychain_for(settings))
        rows = service.list_all() if full else service.list_for(actor.member_id)
        members = {sid: session.get(Member, sid) for sid in {row.subject for row in rows}}
        used = audit.mcp_actor_ids(session)
        sessions: list[AgentSessionOut] = []
        for row in rows:
            member = members.get(row.subject)
            is_agent = member is not None and member.role == Role.agent
            subject_unknown = member is None
            if is_agent or subject_unknown or row.subject in used:
                sessions.append(AgentSessionOut.from_grant(row, service=service, member=member))
        return sessions
