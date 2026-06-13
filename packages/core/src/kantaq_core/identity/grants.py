"""Capability-grant issuance and verification in the runtime (E06-T5/T6).

A grant is the fine, per-session authorization layer (D-03 — it never widens
what RLS allows, and it never widens what the member's **role** allows
either: every requested verb must pass the PRD §11 matrix for the subject,
agents by their token scopes). The runtime's device key signs it via MOD-17;
verification is the protocol's offline check plus this store's knowledge
(roots from the ``devices`` table, revocations from ``revoked_at``).

Lifetimes (FR-E06-5): default 1 hour; **24 hours is the hard ceiling for
everyone in v0.1** — the spec demands it for agents, and humans get the same
defensive cap until backend-issued grants land (v0.2, FR-E06-6).

Rotation invalidates derived grants (E06-T6): every grant records the
``token_id`` it was authorized under; ``rotate_token``/``revoke_member``
call ``revoke_grants_for_member``, so a rotated or revoked credential takes
its grants down with it within the same transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_core.identity.devices import (
    device_private_key,
    local_device,
    verification_roots,
)
from kantaq_core.identity.keychain import Keychain
from kantaq_core.identity.roles import Action, Role, can
from kantaq_core.identity.service import IdentityError, MemberNotFoundError
from kantaq_db.models import CapabilityGrantRow, Member, Token
from kantaq_protocol import (
    GRANT_INVALID_VALIDITY,
    GRANT_REVOKED,
    CapabilityGrant,
    GrantVerification,
    sign_grant,
    verify_grant,
)

DEFAULT_GRANT_TTL_SECONDS = 3600  # 1 hour (FR-E06-5 default)
MAX_GRANT_TTL_SECONDS = 86_400  # 24 hours (FR-E06-5 ceiling, applied to all)


class GrantDeniedError(IdentityError):
    """The requested grant would exceed what the subject's role allows."""


class GrantNotFoundError(IdentityError):
    def __init__(self, grant_id: str) -> None:
        super().__init__(f"no such grant: {grant_id}")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _unix(ts: datetime) -> int:
    # Store timestamps are naive UTC (the MOD-03 rule); make that explicit
    # before epoch conversion so the signed integers are timezone-proof.
    return int(ts.replace(tzinfo=UTC).timestamp())


def _to_protocol(row: CapabilityGrantRow) -> CapabilityGrant:
    return CapabilityGrant(
        grant_id=row.id,
        subject=row.subject,
        issuer=row.issuer,
        resource=row.resource,
        verbs=tuple(row.verbs),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revokes=row.revokes,
        sig=row.sig,
    )


class GrantService:
    """Issue, verify, list, and revoke grants on one session."""

    def __init__(
        self,
        session: Session,
        keychain: Keychain,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._keychain = keychain
        self._now = now or _utcnow

    # ------------------------------------------------------------------ issue

    def issue(
        self,
        *,
        subject_member_id: str,
        resource: str,
        verbs: Sequence[str],
        ttl_seconds: int | None = None,
        actor_id: str,
    ) -> CapabilityGrantRow:
        """A signed, role-derived, short-lived grant (E06-T5, FR-E06-4/5).

        Fails closed: an unknown verb, a verb outside the subject's role (or
        an agent's token scopes), a TTL outside (0, 24 h], a missing device
        key, or a revoked subject all refuse — nothing is written.
        """
        member = self._session.get(Member, subject_member_id)
        if member is None:
            raise MemberNotFoundError(subject_member_id)
        # "invited" is grantable — the invite → mint token → issue grant flow
        # all happens before the member's first authenticated call; only a
        # revoked membership is a dead end.
        if member.status == "revoked":
            raise GrantDeniedError(f"member {member.email} is revoked")

        scopes = self._active_scopes(member)
        if not verbs:
            raise GrantDeniedError("a grant needs at least one verb")
        for verb in verbs:
            try:
                action = Action(verb)
            except ValueError as exc:
                raise GrantDeniedError(f"unknown verb: {verb!r}") from exc
            if not can(member.role, action, scopes=scopes):
                # Grants derive from the role; they never widen it (D-03).
                raise GrantDeniedError(f"role {member.role!r} may not be granted {verb!r}")

        ttl = DEFAULT_GRANT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise GrantDeniedError("grant ttl must be positive")
        if ttl > MAX_GRANT_TTL_SECONDS:
            raise GrantDeniedError(
                f"grant ttl exceeds the {MAX_GRANT_TTL_SECONDS} s (24 h) ceiling"
            )

        device = local_device(self._session, self._keychain)
        seed = device_private_key(self._keychain)
        if device is None or seed is None or device.revoked_at is not None:
            raise GrantDeniedError("this runtime has no active device key (boot first)")

        ts = self._now()
        issued_at = _unix(ts)
        row = CapabilityGrantRow(
            subject=member.id,
            issuer=device.id,
            resource=resource,
            verbs=list(verbs),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
            token_id=self._active_token_id(member.id),
            created_at=ts,
            updated_at=ts,
        )
        signed = sign_grant(_to_protocol(row), seed)
        row.sig = signed.sig
        self._session.add(row)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=actor_id,
            action="grant.issue",
            source="app",
            object_ref=f"capability_grants/{row.id}",
            after=audit.snapshot(row),
            now=ts,
        )
        return row

    # ----------------------------------------------------------------- verify

    def verify(self, row: CapabilityGrantRow) -> GrantVerification:
        """The MOD-17 offline check plus this store's roots and revocations.

        Store-level backstops (E27 review): even a *validly signed* row fails
        if its lifetime exceeds the 24 h ceiling or its subject is revoked —
        a synced or imported row must not out-privilege what issuance allows.
        """
        revoked = set(
            self._session.exec(
                select(CapabilityGrantRow.id).where(col(CapabilityGrantRow.revoked_at).is_not(None))
            ).all()
        )
        result = verify_grant(
            _to_protocol(row),
            verification_roots(self._session),
            now=_unix(self._now()),
            revoked_ids=revoked,
        )
        if not result.ok:
            return result
        if row.expires_at - row.issued_at > MAX_GRANT_TTL_SECONDS:
            return GrantVerification(False, GRANT_INVALID_VALIDITY)
        subject = self._session.get(Member, row.subject)
        if subject is None or subject.status == "revoked":
            return GrantVerification(False, GRANT_REVOKED)
        return result

    # ------------------------------------------------------------ list/revoke

    def list_for(self, member_id: str) -> list[CapabilityGrantRow]:
        statement = (
            select(CapabilityGrantRow)
            .where(CapabilityGrantRow.subject == member_id)
            .order_by(col(CapabilityGrantRow.id).desc())
        )
        return list(self._session.exec(statement).all())

    def get(self, grant_id: str) -> CapabilityGrantRow:
        row = self._session.get(CapabilityGrantRow, grant_id)
        if row is None:
            raise GrantNotFoundError(grant_id)
        return row

    def revoke(self, grant_id: str, *, actor_id: str) -> CapabilityGrantRow:
        row = self.get(grant_id)
        if row.revoked_at is None:
            ts = self._now()
            before = audit.snapshot(row)
            row.revoked_at = ts
            row.updated_at = ts
            self._session.add(row)
            audit.write(
                self._session,
                actor_id=actor_id,
                action="grant.revoke",
                source="app",
                object_ref=f"capability_grants/{row.id}",
                before=before,
                after=audit.snapshot(row),
                now=ts,
            )
        return row

    def _active_scopes(self, member: Member) -> list[str]:
        if member.role != Role.agent.value:
            return []
        return [scope for scopes in self._active_token_scopes(member.id) for scope in scopes]

    def _active_token_scopes(self, member_id: str) -> list[list[str]]:
        statement = select(Token).where(
            Token.member_id == member_id, col(Token.revoked_at).is_(None)
        )
        return [list(token.scopes) for token in self._session.exec(statement).all()]

    def _active_token_id(self, member_id: str) -> str | None:
        statement = select(Token).where(
            Token.member_id == member_id, col(Token.revoked_at).is_(None)
        )
        token = self._session.exec(statement).first()
        return token.id if token is not None else None


def revoke_grants_for_member(
    session: Session,
    member_id: str,
    *,
    actor_id: str,
    now: datetime | None = None,
) -> int:
    """Revoke every live grant of a member (rotation/revocation hook, E06-T6).

    Called inside ``rotate_token`` and ``revoke_member`` transactions: when
    the credential a grant derived from dies, the grant dies with it.
    """
    ts = now or _utcnow()
    statement = select(CapabilityGrantRow).where(
        CapabilityGrantRow.subject == member_id,
        col(CapabilityGrantRow.revoked_at).is_(None),
    )
    rows = list(session.exec(statement).all())
    for row in rows:
        before = audit.snapshot(row)
        row.revoked_at = ts
        row.updated_at = ts
        session.add(row)
        audit.write(
            session,
            actor_id=actor_id,
            action="grant.revoke",
            source="app",
            object_ref=f"capability_grants/{row.id}",
            before=before,
            after=audit.snapshot(row),
            now=ts,
        )
    return len(rows)


def revoke_grants_for_device(
    session: Session,
    device_id: str,
    *,
    actor_id: str,
    now: datetime | None = None,
) -> int:
    """Revoke every live grant a device issued (device decommission, E20-T2).

    A revoked device already leaves ``verification_roots``, so its grants fail
    closed as ``unknown_root``; this makes that explicit and audited, mirroring
    ``revoke_grants_for_member`` but keyed on the *issuing device* rather than
    the subject member.
    """
    ts = now or _utcnow()
    statement = select(CapabilityGrantRow).where(
        CapabilityGrantRow.issuer == device_id,
        col(CapabilityGrantRow.revoked_at).is_(None),
    )
    rows = list(session.exec(statement).all())
    for row in rows:
        before = audit.snapshot(row)
        row.revoked_at = ts
        row.updated_at = ts
        session.add(row)
        audit.write(
            session,
            actor_id=actor_id,
            action="grant.revoke",
            source="app",
            object_ref=f"capability_grants/{row.id}",
            before=before,
            after=audit.snapshot(row),
            now=ts,
        )
    return len(rows)
