"""Grants API: issue, list, verify, revoke capability grants (E06-T5/T6, MOD-06).

The runtime half of the v0.1 grant slice (backend-side enforcement is Sprint
4, E24-T5). Permissions mirror the token surface: a human member may issue a
grant **for themselves** (it derives from their own role and can never widen
it); issuing or revoking for *another* member — including agents — needs
``tokens.rotate``, the §11 credential-management permission Maintainers hold.
Agents can never issue grants: propose-first means an agent's authority only
ever shrinks from its token, and a grant-issuing agent could mint itself
fresh lifetimes forever.

SEC contract: responses carry the verify key id and signature (public
material) — never any private key. The no-secret-leak test scans every
response and the OpenAPI document for the device seed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import (
    Action,
    GrantDeniedError,
    GrantNotFoundError,
    GrantService,
    MemberNotFoundError,
    Role,
    VerifiedActor,
    can,
)
from kantaq_db.models import CapabilityGrantRow
from kantaq_runtime.auth import get_engine_dep, keychain_for, require_actor
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/grants", tags=["grants"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]


class GrantIn(BaseModel):
    resource: str = Field(min_length=1)
    verbs: list[str] = Field(min_length=1)
    member_id: str | None = None  # defaults to the caller
    ttl_seconds: int | None = None


class GrantOut(BaseModel):
    id: str
    subject: str
    issuer: str
    resource: str
    verbs: list[str]
    issued_at: int
    expires_at: int
    revoked_at: str | None
    sig: str | None
    valid: bool
    reason: str

    @classmethod
    def from_row(cls, row: CapabilityGrantRow, *, service: GrantService) -> GrantOut:
        verification = service.verify(row)
        return cls(
            id=row.id,
            subject=row.subject,
            issuer=row.issuer,
            resource=row.resource,
            verbs=list(row.verbs),
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at.isoformat() if row.revoked_at is not None else None,
            sig=row.sig,
            valid=verification.ok,
            reason=verification.reason,
        )


def _service(session: Session, request: Request) -> GrantService:
    settings: Settings = request.app.state.settings
    return GrantService(session, keychain_for(settings))


def _require_self_or_credential_admin(actor: VerifiedActor, member_id: str) -> None:
    if actor.member_id == member_id:
        return
    if not can(actor.role, Action.tokens_rotate, scopes=list(actor.scopes)):
        raise HTTPException(
            status_code=403,
            detail="issuing or revoking another member's grants needs tokens.rotate",
        )


@router.post("", response_model=GrantOut, status_code=201)
def issue_grant(body: GrantIn, actor: AnyActor, engine: EngineDep, request: Request) -> GrantOut:
    # Propose-first (FR-E09-4): an agent's authority comes from its token and
    # only ever narrows; it cannot mint itself fresh grants or lifetimes.
    if actor.role == Role.agent:
        raise HTTPException(status_code=403, detail="agents may not issue grants")
    subject = body.member_id or actor.member_id
    _require_self_or_credential_admin(actor, subject)
    with Session(engine) as session:
        service = _service(session, request)
        try:
            row = service.issue(
                subject_member_id=subject,
                resource=body.resource,
                verbs=body.verbs,
                ttl_seconds=body.ttl_seconds,
                actor_id=actor.member_id,
            )
        except MemberNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GrantDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        session.commit()
        session.refresh(row)
        return GrantOut.from_row(row, service=service)


@router.get("", response_model=list[GrantOut])
def list_grants(
    actor: AnyActor, engine: EngineDep, request: Request, member: str | None = None
) -> list[GrantOut]:
    subject = member or actor.member_id
    # Same boundary as issue/revoke (E27 review): grants reveal a member's
    # resources and verbs — no cross-member enumeration without tokens.rotate.
    _require_self_or_credential_admin(actor, subject)
    with Session(engine) as session:
        service = _service(session, request)
        return [GrantOut.from_row(row, service=service) for row in service.list_for(subject)]


@router.post("/{grant_id}/revoke", response_model=GrantOut)
def revoke_grant(grant_id: str, actor: AnyActor, engine: EngineDep, request: Request) -> GrantOut:
    with Session(engine) as session:
        service = _service(session, request)
        try:
            row = service.get(grant_id)
        except GrantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_self_or_credential_admin(actor, row.subject)
        row = service.revoke(grant_id, actor_id=actor.member_id)
        session.commit()
        session.refresh(row)
        return GrantOut.from_row(row, service=service)
