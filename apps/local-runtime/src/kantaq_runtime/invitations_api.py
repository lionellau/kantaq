"""Invitations API: craft + accept the signed ``twp://invite`` bundle (E06-T8).

The protocol-correct onboarding (DEBT-04), the alternative to the Supabase
magic-link: a Maintainer crafts a device-signed invite (``POST /v1/invitations``)
and shares the ``twp://invite/<payload>`` URI out of band; the invitee's runtime
accepts it (``POST /v1/invitations/accept``), which **verifies the signature
against the issuer device root** and, only on a clean verify, admits the member.
A forged, tampered, expired, or unknown-root invite is refused with its reason —
the invite's signature is the cross-workspace authorization, so accept needs only
a valid local session, not ``members.invite``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_core.identity import (
    ROLE_PERMISSIONS,
    Action,
    IdentityError,
    IdentityService,
    Role,
    VerifiedActor,
    device_private_key,
    local_device,
    verification_roots,
)
from kantaq_db import Member, new_ulid
from kantaq_protocol import (
    Invite,
    SchemaViolation,
    decode_invite_uri,
    encode_invite_uri,
    sign_invite,
    verify_invite,
)
from kantaq_runtime.auth import get_engine_dep, keychain_for, require_action, require_actor
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/invitations", tags=["invitations"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]
InviterActor = Annotated[VerifiedActor, Depends(require_action(Action.members_invite))]

# An invite is short-lived: it is a one-time join window, not a standing grant.
DEFAULT_INVITE_TTL_SECONDS = 7 * 86_400  # 7 days


class CraftIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Role = Role.member
    ttl_seconds: int = Field(default=DEFAULT_INVITE_TTL_SECONDS, gt=0, le=30 * 86_400)


class CraftOut(BaseModel):
    invite: str  # the twp://invite/<payload> URI, shared out of band
    invite_id: str
    expires_at: int


class AcceptIn(BaseModel):
    invite: str = Field(min_length=1)


class AcceptOut(BaseModel):
    member_id: str
    email: str
    role: str
    status: str
    token: str | None  # the one-time local token for a newly admitted member; None on re-accept
    reused: bool


def _now_unix() -> int:
    return int(datetime.now(UTC).timestamp())


@router.post("", response_model=CraftOut, status_code=201)
def craft_invitation(
    body: CraftIn, actor: InviterActor, engine: EngineDep, request: Request
) -> CraftOut:
    """Build + device-sign a ``twp://invite`` for a new member (Maintainer+)."""
    if body.role == Role.agent:
        raise HTTPException(
            status_code=400, detail="agents onboard via token scopes, not a twp://invite"
        )
    settings: Settings = request.app.state.settings
    keychain = keychain_for(settings)
    with Session(engine) as session:
        device = local_device(session, keychain)
        seed = device_private_key(keychain)
        if device is None or seed is None or device.revoked_at is not None:
            raise HTTPException(status_code=409, detail="this runtime has no active device key")
        inviter = session.get(Member, actor.member_id)
        if inviter is None:
            raise HTTPException(status_code=404, detail="inviting member not found")
        # Owner-tier guard (mirrors the Supabase RLS Owner rule): only an Owner
        # may craft an Owner invite — a Maintainer holds members.invite but must
        # not be able to mint an Owner and take the workspace over (SEC review).
        # Lower human roles a Maintainer may invite freely.
        if body.role == Role.owner and inviter.role != Role.owner.value:
            raise HTTPException(status_code=403, detail="only an Owner may invite another Owner")
        issued = _now_unix()
        # The invite carries the role's full capability as the grant scope; the
        # accepted member's actual signed grant is still role-derived locally
        # (ensure_member_grant), so the invite never widens the role (D-03).
        verbs = tuple(sorted(a.value for a in ROLE_PERMISSIONS.get(body.role, frozenset())))
        invite = Invite(
            invite_id=new_ulid(),
            workspace_id=inviter.workspace_id,
            subject_email=body.email,
            role=body.role.value,
            resource=inviter.workspace_id,
            verbs=verbs or ("tickets.read",),
            issuer=device.id,
            issued_at=issued,
            expires_at=issued + body.ttl_seconds,
        )
        signed = sign_invite(invite, seed)
        audit.write(
            session,
            actor_id=actor.member_id,
            action="invite.craft",
            source="app",
            object_ref=f"invitations/{invite.invite_id}",
            after={"subject_email": body.email, "role": body.role.value},
        )
        session.commit()
        return CraftOut(
            invite=encode_invite_uri(signed),
            invite_id=invite.invite_id,
            expires_at=invite.expires_at,
        )


@router.post("/accept", response_model=AcceptOut)
def accept_invitation(body: AcceptIn, actor: AnyActor, engine: EngineDep) -> AcceptOut:
    """Verify a ``twp://invite`` against the device root and admit the member."""
    try:
        invite = decode_invite_uri(body.invite)
    except SchemaViolation as exc:
        raise HTTPException(status_code=400, detail=f"malformed invite: {exc}") from exc

    with Session(engine) as session:
        result = verify_invite(invite, verification_roots(session), now=_now_unix())
        if not result.ok:
            # forged / expired / unknown_root / missing_signature / not_yet_valid
            raise HTTPException(status_code=400, detail=f"invite rejected: {result.reason}")

        # The role must be a known, non-agent role (agents onboard via token
        # scopes; the craft endpoint blocks Agent, accept enforces it too so a
        # directly-signed invite can't slip an Agent or garbage role through).
        try:
            invite_role = Role(invite.role)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invite carries an unknown role: {invite.role!r}"
            ) from exc
        if invite_role is Role.agent:
            raise HTTPException(
                status_code=400, detail="agents onboard via token scopes, not a twp://invite"
            )
        # The invite must be for THIS runtime's workspace (defence beyond the
        # root check, which already bars an unknown issuer device).
        caller = session.get(Member, actor.member_id)
        if caller is None or invite.workspace_id != caller.workspace_id:
            raise HTTPException(status_code=400, detail="invite is for a different workspace")

        # Idempotent: an invite re-accepted (or for an already-known member) returns
        # the existing member rather than admitting a duplicate.
        existing = session.exec(
            select(Member).where(
                col(Member.workspace_id) == invite.workspace_id,
                col(Member.email) == invite.subject_email.lower(),
            )
        ).first()
        if existing is not None:
            if existing.status == "revoked":
                # Fail loudly (not a silent 200) — and don't leak revoked-membership
                # as an oracle; re-admission needs the explicit un-revoke flow.
                raise HTTPException(
                    status_code=409, detail="member is revoked; re-admission is not via invite"
                )
            return AcceptOut(
                member_id=existing.id,
                email=existing.email,
                role=existing.role,
                status=existing.status,
                token=None,
                reused=True,
            )

        try:
            minted = IdentityService(session).invite(email=invite.subject_email, role=invite_role)
        except IdentityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        member = IdentityService(session).get_member(minted.member_id)
        audit.write(
            session,
            actor_id=member.id,
            action="invite.accept",
            source="app",
            object_ref=f"invitations/{invite.invite_id}",
            after={"member_id": member.id, "issuer": invite.issuer},
        )
        session.commit()
        return AcceptOut(
            member_id=member.id,
            email=member.email,
            role=member.role,
            status=member.status,
            token=minted.plaintext,
            reused=False,
        )
