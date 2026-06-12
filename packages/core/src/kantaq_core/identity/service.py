"""Member and token lifecycle (E06-T3: invite, list, revoke, rotate).

Pure domain logic over the MOD-02 ``members``/``tokens`` tables, shared by the
runtime's ``/v1/members`` API and the ``kantaq token`` CLI. The Settings→Members
UI (Sprint 2) calls the same API. Magic-link invites need the Supabase backend
(E24); the local path mints a one-time bearer token instead (FR-E06-2 allows
either), and the two onboarding paths converge in v0.2 (DEBT-04).

Audit rows for these writes land with E07 (MOD-07 owns the audit seam).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from kantaq_core.identity.roles import Role
from kantaq_core.identity.tokens import mint_token
from kantaq_db.models import Member, Token, Workspace, new_id

DEFAULT_WORKSPACE_NAME = "Local Workspace"
BOOTSTRAP_OWNER_EMAIL = "owner@local"


class IdentityError(Exception):
    """Base class for identity domain errors."""


class MemberNotFoundError(IdentityError):
    def __init__(self, member_id: str) -> None:
        super().__init__(f"no such member: {member_id}")
        self.member_id = member_id


class LastOwnerError(IdentityError):
    def __init__(self) -> None:
        super().__init__("cannot revoke the last active Owner of the workspace")


@dataclass(frozen=True)
class MintedToken:
    """A freshly minted bearer token. ``plaintext`` is shown once, never stored."""

    member_id: str
    token_id: str
    plaintext: str


class IdentityService:
    """Member lifecycle over one SQLModel session (caller owns the engine)."""

    def __init__(self, session: Session, *, now: Callable[[], datetime] | None = None) -> None:
        self._session = session
        self._now = now or (lambda: datetime.now(UTC))

    # -- queries ------------------------------------------------------------

    def list_members(self) -> list[Member]:
        statement = select(Member).order_by(Member.created_at, Member.id)  # type: ignore[arg-type]
        return list(self._session.exec(statement).all())

    def get_member(self, member_id: str) -> Member:
        member = self._session.get(Member, member_id)
        if member is None:
            raise MemberNotFoundError(member_id)
        return member

    def has_members(self) -> bool:
        return self._session.exec(select(Member).limit(1)).first() is not None

    # -- lifecycle ----------------------------------------------------------

    def bootstrap_owner(self, *, email: str = BOOTSTRAP_OWNER_EMAIL) -> MintedToken | None:
        """First boot in an empty database: create the Owner and their token.

        Solo mode has no human login (D-06), but the runtime is still
        token-gated, so somebody must hold the first token. Returns None when
        members already exist (boot is idempotent).
        """
        if self.has_members():
            return None
        member = Member(
            workspace_id=self._workspace().id,
            email=email,
            role=Role.owner.value,
            status="active",
        )
        self._session.add(member)
        minted = self._mint_for(member)
        self._session.commit()
        return minted

    def invite(self, *, email: str, role: Role, scopes: list[str] | None = None) -> MintedToken:
        """Create a member in ``invited`` status and mint their one-time token.

        ``scopes`` only applies to ``Agent`` members (token-scoped, PRD §11);
        human tokens carry no scopes — their role decides.
        """
        if role is not Role.agent and scopes:
            raise IdentityError("scopes are only valid for Agent members")
        member = Member(
            workspace_id=self._workspace().id,
            email=email,
            role=role.value,
            status="invited",
        )
        self._session.add(member)
        minted = self._mint_for(member, scopes=scopes or [])
        self._session.commit()
        return minted

    def revoke_member(self, member_id: str) -> Member:
        """Revoke a member and every token they hold. Guards the last Owner."""
        member = self.get_member(member_id)
        if member.role == Role.owner.value and self._active_owner_count() <= 1:
            raise LastOwnerError()
        member.status = "revoked"
        member.updated_at = self._now()
        self._session.add(member)
        self._revoke_tokens(member_id)
        self._revoke_devices(member_id)
        self._session.commit()
        self._session.refresh(member)
        return member

    def rotate_token(self, member_id: str) -> MintedToken:
        """Revoke the member's active tokens and mint a fresh one."""
        member = self.get_member(member_id)
        if member.status == "revoked":
            raise IdentityError(f"member {member_id} is revoked; invite them again instead")
        scopes = self._active_scopes(member_id)
        self._revoke_tokens(member_id)
        minted = self._mint_for(member, scopes=scopes)
        self._session.commit()
        return minted

    # -- internals ----------------------------------------------------------

    def _workspace(self) -> Workspace:
        statement = select(Workspace).order_by(Workspace.created_at, Workspace.id)  # type: ignore[arg-type]
        workspace = self._session.exec(statement).first()
        if workspace is None:
            workspace = Workspace(name=DEFAULT_WORKSPACE_NAME, visibility="local")
            self._session.add(workspace)
            self._session.flush()
        return workspace

    def _mint_for(self, member: Member, *, scopes: list[str] | None = None) -> MintedToken:
        token_id = new_id()
        plaintext, hashed = mint_token(token_id)
        self._session.add(
            Token(id=token_id, member_id=member.id, hashed=hashed, scopes=scopes or [])
        )
        return MintedToken(member_id=member.id, token_id=token_id, plaintext=plaintext)

    def _revoke_tokens(self, member_id: str) -> None:
        statement = select(Token).where(Token.member_id == member_id)
        for token in self._session.exec(statement).all():
            if token.revoked_at is None:
                token.revoked_at = self._now()
                token.updated_at = self._now()
                self._session.add(token)
        # E06-T6: the credential a capability grant derived from is dying —
        # its grants die in the same transaction (rotate and revoke both
        # funnel through here). Deferred import: grants.py imports this
        # module for the error types.
        from kantaq_core.identity.grants import revoke_grants_for_member

        revoke_grants_for_member(self._session, member_id, actor_id=member_id, now=self._now())

    def _revoke_devices(self, member_id: str) -> None:
        # E27 review: a revoked member's device must stop being a
        # verification root, or their old key keeps validating grants.
        from kantaq_db.models import Device

        statement = select(Device).where(Device.member_id == member_id)
        for device in self._session.exec(statement).all():
            if device.revoked_at is None:
                device.revoked_at = self._now()
                device.updated_at = self._now()
                self._session.add(device)

    def _active_scopes(self, member_id: str) -> list[str]:
        statement = (
            select(Token)
            .where(Token.member_id == member_id)
            .order_by(Token.created_at.desc(), Token.id.desc())  # type: ignore[attr-defined]
        )
        for token in self._session.exec(statement).all():
            if token.revoked_at is None:
                return list(token.scopes)
        return []

    def _active_owner_count(self) -> int:
        statement = select(Member).where(
            Member.role == Role.owner.value, Member.status != "revoked"
        )
        return len(list(self._session.exec(statement).all()))
