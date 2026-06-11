"""Members API: invite, list, revoke, rotate (E06-T3, FR-E06-2).

The Settings→Members UI (Sprint 2) and the CLI both drive these routes. Token
plaintext appears exactly once — in the invite/rotate response that minted it —
and no response ever carries hash material (NFR-E06-1). Audit rows for these
writes land with E07 (MOD-07 owns the audit seam).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import (
    Action,
    IdentityError,
    IdentityService,
    LastOwnerError,
    MemberNotFoundError,
    Role,
    TokenVerifier,
    VerifiedActor,
    can,
)
from kantaq_db.models import Member
from kantaq_runtime.auth import get_engine_dep, get_verifier, require_action, require_actor

router = APIRouter(prefix="/v1/members", tags=["members"])

# Annotated dependency aliases: one per permission level (and the shared
# engine/verifier), so each route signature reads as its access rule.
EngineDep = Annotated[Engine, Depends(get_engine_dep)]
VerifierDep = Annotated[TokenVerifier, Depends(get_verifier)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.members_read))]
InviterActor = Annotated[VerifiedActor, Depends(require_action(Action.members_invite))]
RevokerActor = Annotated[VerifiedActor, Depends(require_action(Action.members_revoke))]


class MemberOut(BaseModel):
    """A member row, minus anything secret."""

    id: str
    workspace_id: str
    email: str
    role: str
    status: str
    created_at: datetime

    @classmethod
    def from_row(cls, member: Member) -> MemberOut:
        return cls(
            id=member.id,
            workspace_id=member.workspace_id,
            email=member.email,
            role=member.role,
            status=member.status,
            created_at=member.created_at,
        )


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Role = Role.member
    scopes: list[str] = Field(default_factory=list)


class InviteOut(BaseModel):
    member: MemberOut
    token: str  # shown once; only the Argon2id hash is stored


class RotateOut(BaseModel):
    member_id: str
    token: str  # shown once


@router.get("", response_model=list[MemberOut])
def list_members(actor: ReaderActor, engine: EngineDep) -> list[MemberOut]:
    with Session(engine) as session:
        return [MemberOut.from_row(m) for m in IdentityService(session).list_members()]


@router.post("/invite", response_model=InviteOut, status_code=201)
def invite_member(body: InviteIn, actor: InviterActor, engine: EngineDep) -> InviteOut:
    with Session(engine) as session:
        service = IdentityService(session)
        try:
            minted = service.invite(email=body.email, role=body.role, scopes=body.scopes)
        except IdentityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        member = service.get_member(minted.member_id)
        return InviteOut(member=MemberOut.from_row(member), token=minted.plaintext)


@router.post("/{member_id}/revoke", response_model=MemberOut)
def revoke_member(
    member_id: str, actor: RevokerActor, engine: EngineDep, verifier: VerifierDep
) -> MemberOut:
    with Session(engine) as session:
        try:
            member = IdentityService(session).revoke_member(member_id)
        except MemberNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastOwnerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Same-process sessions stop now; other processes within the TTL (< 5 s).
    verifier.invalidate_member(member_id)
    return MemberOut.from_row(member)


@router.post("/{member_id}/rotate", response_model=RotateOut)
def rotate_token(
    member_id: str, actor: AnyActor, engine: EngineDep, verifier: VerifierDep
) -> RotateOut:
    # Anyone may rotate their own token; rotating another member's needs the
    # tokens.rotate permission (Owner/Maintainer).
    if member_id != actor.member_id and not can(
        actor.role, Action.tokens_rotate, scopes=list(actor.scopes)
    ):
        raise HTTPException(
            status_code=403, detail=f"role {actor.role!r} may not rotate other members' tokens"
        )
    with Session(engine) as session:
        try:
            minted = IdentityService(session).rotate_token(member_id)
        except MemberNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except IdentityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    verifier.invalidate_member(member_id)
    return RotateOut(member_id=minted.member_id, token=minted.plaintext)
