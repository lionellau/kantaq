"""Device identity + grant issuance/verification (E06-T4/T5/T6, Identity profile).

The sprint exit criteria this suite pins:
- a runtime generates and registers its device keypair at boot, idempotently,
  and the private key never appears outside the keychain (criterion 3);
- a grant issued from a member role verifies offline; expired and forged
  grants are rejected (criterion 2, FakeClock-driven — criterion 4);
- a rotated member token invalidates its derived grants (criterion 4).
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core import audit
from kantaq_core.identity import (
    DEFAULT_GRANT_TTL_SECONDS,
    DEVICE_KEY_NAME,
    MAX_GRANT_TTL_SECONDS,
    DeviceNotFoundError,
    GrantDeniedError,
    GrantService,
    IdentityService,
    Role,
    ensure_device,
    ensure_member_grant,
    revoke_device,
    revoke_grants_for_device,
    verification_roots,
)
from kantaq_db.models import AuditEvent, CapabilityGrantRow, Device, EventLog, Member
from kantaq_protocol import GRANT_OK
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.keychain import FakeKeychain


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def keychain() -> FakeKeychain:
    return FakeKeychain()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _now(clock: FakeClock):  # noqa: ANN202 - tiny local helper
    return lambda: clock.now().replace(tzinfo=None)


def _grant_service(session: Session, keychain: FakeKeychain, clock: FakeClock) -> GrantService:
    return GrantService(session, keychain, now=_now(clock))


@pytest.fixture
def owner_id(engine: Engine) -> str:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted.member_id


# ------------------------------------------------------------------ devices


def test_boot_generates_and_registers_a_device(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        device = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        session.commit()
        assert keychain.get(DEVICE_KEY_NAME) is not None
        assert len(device.public_key) == 64
        # The registration is audited.
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
        assert "device.register" in actions


def test_boot_is_idempotent_one_device_per_runtime(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        first = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        session.commit()
        second = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        session.commit()
        assert first.id == second.id
        assert len(session.exec(select(Device)).all()) == 1


def test_a_wiped_replica_reregisters_from_the_keychain_seed(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        first = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        public_key = first.public_key
        session.delete(first)
        session.commit()
    with Session(engine) as session:
        again = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        assert again.public_key == public_key  # the keychain is the identity


def test_private_key_never_lands_in_db_audit_or_event_log(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """Sprint exit criterion 3: the seed exists in the keychain and nowhere else."""
    from kantaq_sync_engine import EventLogSink

    with Session(engine) as session:
        ensure_device(
            session,
            keychain,
            member_id=owner_id,
            sink=EventLogSink(session, owner_id),
            now=_now(clock)(),
        )
        session.commit()
        seed = keychain.get(DEVICE_KEY_NAME)
        assert seed is not None
        device_dump = str([audit.snapshot(r) for r in session.exec(select(Device)).all()])
        audit_dump = str([audit.snapshot(r) for r in session.exec(select(AuditEvent)).all()])
        event_dump = str([(r.collection, r.payload) for r in session.exec(select(EventLog)).all()])
        for dump in (device_dump, audit_dump, event_dump):
            assert seed not in dump


def test_device_registration_emits_a_sync_event(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    from kantaq_sync_engine import EventLogSink

    with Session(engine) as session:
        device = ensure_device(
            session,
            keychain,
            member_id=owner_id,
            sink=EventLogSink(session, owner_id),
            now=_now(clock)(),
        )
        session.commit()
        rows = [r for r in session.exec(select(EventLog)).all() if r.collection == "devices"]
        assert [r.entity_id for r in rows] == [device.id]
        assert rows[0].payload["public_key"] == device.public_key


# ------------------------------------------------------- issue + verify


def test_a_member_grant_issues_and_verifies_offline(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=owner_id,
            resource="workspace/main",
            verbs=["tickets.read", "tickets.write"],
            actor_id=owner_id,
        )
        session.commit()
        result = service.verify(row)
        assert result.ok
        assert result.reason == GRANT_OK
        assert row.expires_at - row.issued_at == DEFAULT_GRANT_TTL_SECONDS
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
        assert "grant.issue" in actions


def test_grants_never_widen_the_role(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        viewer = IdentityService(session).invite(email="v@example.com", role=Role.viewer)
        service = _grant_service(session, keychain, clock)
        with pytest.raises(GrantDeniedError, match="may not be granted"):
            service.issue(
                subject_member_id=viewer.member_id,
                resource="workspace/main",
                verbs=["tickets.write"],  # Viewers read; a grant cannot add writes
                actor_id=owner_id,
            )


def test_unknown_verbs_fail_closed(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        service = _grant_service(session, keychain, clock)
        with pytest.raises(GrantDeniedError, match="unknown verb"):
            service.issue(
                subject_member_id=owner_id,
                resource="workspace/main",
                verbs=["tickets.admin"],
                actor_id=owner_id,
            )


def test_agent_grants_derive_from_token_scopes(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        agent = IdentityService(session).invite(
            email="bot@example.com",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=agent.member_id,
            resource="workspace/main",
            verbs=["tickets.read", "proposals.write"],
            actor_id=owner_id,
        )
        assert service.verify(row).ok
        with pytest.raises(GrantDeniedError, match="may not be granted"):
            service.issue(
                subject_member_id=agent.member_id,
                resource="workspace/main",
                verbs=["tickets.write"],  # outside the token's scopes
                actor_id=owner_id,
            )


def test_a_forged_grant_is_rejected(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=owner_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        row.verbs = ["tickets.read", "members.revoke"]  # widen after signing
        assert service.verify(row).reason == "forged"


def test_an_unknown_or_revoked_device_is_no_root(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        device = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=owner_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        assert service.verify(row).ok
        device.revoked_at = _now(clock)()
        session.add(device)
        session.flush()
        assert device.id not in verification_roots(session)
        assert service.verify(row).reason == "unknown_root"


# ----------------------------------------------------- expiry + rotation


def test_agent_grant_expires_on_schedule(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """Exit criterion 4 first half: FakeClock walks past expiry."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        agent = IdentityService(session).invite(
            email="bot@example.com", role=Role.agent, scopes=["tickets.read"]
        )
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=agent.member_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        assert service.verify(row).ok
        clock.advance(DEFAULT_GRANT_TTL_SECONDS - 1)
        assert service.verify(row).ok
        clock.advance(1)  # exactly at expiry: dead
        assert service.verify(row).reason == "expired"


def test_ttl_ceiling_is_24_hours(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=owner_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            ttl_seconds=MAX_GRANT_TTL_SECONDS,
            actor_id=owner_id,
        )
        assert row.expires_at - row.issued_at == MAX_GRANT_TTL_SECONDS
        with pytest.raises(GrantDeniedError, match="ceiling"):
            service.issue(
                subject_member_id=owner_id,
                resource="workspace/main",
                verbs=["tickets.read"],
                ttl_seconds=MAX_GRANT_TTL_SECONDS + 1,
                actor_id=owner_id,
            )
        with pytest.raises(GrantDeniedError, match="positive"):
            service.issue(
                subject_member_id=owner_id,
                resource="workspace/main",
                verbs=["tickets.read"],
                ttl_seconds=0,
                actor_id=owner_id,
            )


def test_rotating_the_token_invalidates_derived_grants(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """Exit criterion 4 second half: rotation kills the grants it derived."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        agent = IdentityService(session).invite(
            email="bot@example.com", role=Role.agent, scopes=["tickets.read"]
        )
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=agent.member_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        session.commit()
        grant_id = row.id
        assert service.verify(row).ok

        IdentityService(session).rotate_token(agent.member_id)

        refreshed = session.get(CapabilityGrantRow, grant_id)
        assert refreshed is not None
        assert refreshed.revoked_at is not None
        assert service.verify(refreshed).reason == "revoked"
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
        assert "grant.revoke" in actions


def test_revoking_the_member_invalidates_their_grants(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        member = IdentityService(session).invite(email="m@example.com", role=Role.member)
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=member.member_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        session.commit()
        IdentityService(session).revoke_member(member.member_id)
        refreshed = session.get(CapabilityGrantRow, row.id)
        assert refreshed is not None
        assert service.verify(refreshed).reason == "revoked"


def test_issue_refuses_without_a_device_key(
    engine: Engine, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        service = _grant_service(session, FakeKeychain(), clock)  # empty keychain
        with pytest.raises(GrantDeniedError, match="no active device key"):
            service.issue(
                subject_member_id=owner_id,
                resource="workspace/main",
                verbs=["tickets.read"],
                actor_id=owner_id,
            )


def test_issue_refuses_a_revoked_subject(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        member = IdentityService(session).invite(email="m@example.com", role=Role.member)
        IdentityService(session).revoke_member(member.member_id)
        service = _grant_service(session, keychain, clock)
        with pytest.raises(GrantDeniedError, match="revoked"):
            service.issue(
                subject_member_id=member.member_id,
                resource="workspace/main",
                verbs=["tickets.read"],
                actor_id=owner_id,
            )


# ------------------------------------------ adversarial-review backstops


def test_revoking_the_member_also_revokes_their_device_root(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        identity = IdentityService(session)
        member = identity.invite(email="m@example.com", role=Role.member)
        member_keychain = FakeKeychain()
        device = ensure_device(
            session, member_keychain, member_id=member.member_id, now=_now(clock)()
        )
        session.commit()
        assert device.id in verification_roots(session)
        identity.revoke_member(member.member_id)
        assert device.id not in verification_roots(session)


def test_a_validly_signed_overlong_grant_still_fails_verify(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """A synced/imported row cannot out-privilege issuance (E27 backstop)."""
    from kantaq_core.identity import device_private_key
    from kantaq_core.identity.grants import _to_protocol
    from kantaq_db.models import CapabilityGrantRow
    from kantaq_protocol import sign_grant

    with Session(engine) as session:
        device = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        seed = device_private_key(keychain)
        assert seed is not None
        ts = _now(clock)()
        issued = int(ts.replace(tzinfo=__import__("datetime").UTC).timestamp())
        row = CapabilityGrantRow(
            subject=owner_id,
            issuer=device.id,
            resource="workspace/main",
            verbs=["tickets.read"],
            issued_at=issued,
            expires_at=issued + MAX_GRANT_TTL_SECONDS + 3600,  # signed, but over ceiling
            created_at=ts,
            updated_at=ts,
        )
        row.sig = sign_grant(_to_protocol(row), seed).sig
        session.add(row)
        session.flush()
        service = _grant_service(session, keychain, clock)
        assert service.verify(row).reason == "invalid_validity"


def test_a_grant_for_a_revoked_subject_fails_verify(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        member = IdentityService(session).invite(email="m@example.com", role=Role.member)
        service = _grant_service(session, keychain, clock)
        row = service.issue(
            subject_member_id=member.member_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        session.commit()
        IdentityService(session).revoke_member(member.member_id)
        # Doubly dead (rotation hook + subject backstop) — and provably dead
        # even if the revocation hook were somehow skipped.
        assert service.verify(row).reason == "revoked"


# ----------------------------------------------------- device decommission


def test_revoke_device_audits_and_leaves_the_root_map(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        device = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        session.commit()
        assert device.id in verification_roots(session)

        revoked = revoke_device(session, device.id, actor_id=owner_id, now=_now(clock)())
        session.commit()
        assert revoked.revoked_at is not None
        # Once revoked it is no longer a verification root.
        assert device.id not in verification_roots(session)
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
        assert actions.count("device.revoke") == 1


def test_revoke_device_is_idempotent(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        device = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        session.commit()
        revoke_device(session, device.id, actor_id=owner_id, now=_now(clock)())
        revoke_device(session, device.id, actor_id=owner_id, now=_now(clock)())
        session.commit()
        # The second call is a no-op: exactly one revoke audit row.
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
        assert actions.count("device.revoke") == 1


def test_revoke_unknown_device_raises(engine: Engine, owner_id: str) -> None:
    with Session(engine) as session, pytest.raises(DeviceNotFoundError):
        revoke_device(session, "no-such-device", actor_id=owner_id)


def test_revoke_grants_for_device_cascades_only_its_own(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    with Session(engine) as session:
        device = ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        service = _grant_service(session, keychain, clock)
        grant = service.issue(
            subject_member_id=owner_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            actor_id=owner_id,
        )
        session.commit()

        count = revoke_grants_for_device(session, device.id, actor_id=owner_id, now=_now(clock)())
        session.commit()
        assert count == 1
        session.refresh(grant)
        assert grant.revoked_at is not None
        # A second pass finds nothing live to revoke.
        assert revoke_grants_for_device(session, device.id, actor_id=owner_id) == 0


# ----------------------------------------------- self-grant (E04-T4 signing)


def test_ensure_member_grant_issues_role_scoped_signed_grant(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """A member's runtime mints a signed self-grant scoped to its workspace,
    carrying the role's full capability — the policy_ref every signed event
    rides (E04-T4). It verifies offline against the device root."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        workspace_id = session.get(Member, owner_id).workspace_id  # type: ignore[union-attr]
        grant = ensure_member_grant(session, keychain, owner_id, now=_now(clock))
        session.commit()

        assert grant.subject == owner_id
        assert grant.resource == workspace_id
        assert grant.sig is not None
        # Owner holds the full matrix; the grant never widens it (D-03).
        assert "tickets.write" in grant.verbs
        assert "members.invite" in grant.verbs
        assert _grant_service(session, keychain, clock).verify(grant).ok
        # 24 h TTL keeps the policy_ref stable for a day rather than per-hour.
        assert grant.expires_at - grant.issued_at == MAX_GRANT_TTL_SECONDS


def test_ensure_member_grant_is_idempotent_while_live(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """A live self-grant is reused, not re-minted — the policy_ref is stable
    and the log does not fill with a grant per write."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        first = ensure_member_grant(session, keychain, owner_id, now=_now(clock))
        session.commit()
        second = ensure_member_grant(session, keychain, owner_id, now=_now(clock))
        session.commit()
        assert first.id == second.id
        assert len(_grant_service(session, keychain, clock).list_for(owner_id)) == 1


def test_ensure_member_grant_reissues_after_expiry(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """Once the live grant expires, the next write mints a fresh one."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        first = ensure_member_grant(session, keychain, owner_id, now=_now(clock))
        session.commit()
        clock.advance(MAX_GRANT_TTL_SECONDS + 1)
        second = ensure_member_grant(session, keychain, owner_id, now=_now(clock))
        session.commit()
        assert first.id != second.id


def test_ensure_member_grant_without_device_fails_closed(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """No device key, no grant — issuance fails closed rather than minting an
    unsigned capability (the cutover's local invariant, E04-T4)."""
    with Session(engine) as session, pytest.raises(GrantDeniedError):
        ensure_member_grant(session, keychain, owner_id, now=_now(clock))


def test_ensure_member_grant_for_agent_uses_token_scopes(
    engine: Engine, keychain: FakeKeychain, clock: FakeClock, owner_id: str
) -> None:
    """An agent's self-grant derives from its token scopes, not a role row —
    it never widens what the token allows (D-03)."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=owner_id, now=_now(clock)())
        agent = IdentityService(session).invite(
            email="agent@example.com", role=Role.agent, scopes=["tickets.write"]
        )
        session.commit()
        grant = ensure_member_grant(session, keychain, agent.member_id, now=_now(clock))
        session.commit()
        assert grant.verbs == ["tickets.write"]
        assert grant.subject == agent.member_id
        assert _grant_service(session, keychain, clock).verify(grant).ok
