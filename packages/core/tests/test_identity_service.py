"""IdentityService lifecycle: bootstrap, invite, list, revoke, rotate (E06-T3)."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import (
    IdentityError,
    IdentityService,
    LastOwnerError,
    MemberNotFoundError,
    Role,
    TokenVerifier,
)
from kantaq_db.models import Member, Token


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def test_bootstrap_creates_owner_workspace_and_token(engine: Engine) -> None:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    actor = TokenVerifier(engine).verify(minted.plaintext)
    assert actor is not None
    assert actor.role == "Owner"


def test_bootstrap_is_idempotent(engine: Engine) -> None:
    with Session(engine) as session:
        assert IdentityService(session).bootstrap_owner() is not None
    with Session(engine) as session:
        assert IdentityService(session).bootstrap_owner() is None  # second boot: no-op
        assert len(IdentityService(session).list_members()) == 1


def test_invite_creates_invited_member_with_working_token(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        minted = service.invite(email="new@team.dev", role=Role.member)
        member = service.get_member(minted.member_id)
        assert member.status == "invited"
        assert member.role == "Member"
    assert TokenVerifier(engine).verify(minted.plaintext) is not None


def test_invite_agent_carries_scopes_humans_may_not(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        minted = service.invite(email="bot@team.dev", role=Role.agent, scopes=["members.read"])
        with Session(engine) as check:
            token = check.get(Token, minted.token_id)
            assert token is not None
            assert token.scopes == ["members.read"]
        with pytest.raises(IdentityError, match="Agent"):
            service.invite(email="human@team.dev", role=Role.member, scopes=["members.read"])


def test_rotate_rejects_old_token_and_accepts_new(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        old = service.bootstrap_owner()
        assert old is not None
        new = service.rotate_token(old.member_id)
    verifier = TokenVerifier(engine)
    assert verifier.verify(old.plaintext) is None  # rotate → old rejected
    assert verifier.verify(new.plaintext) is not None
    with Session(engine) as session:
        stored = session.get(Token, old.token_id)
        assert stored is not None
        assert stored.revoked_at is not None  # kept, not deleted


def test_rotate_preserves_agent_scopes(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        agent = service.invite(email="bot@team.dev", role=Role.agent, scopes=["members.read"])
        rotated = service.rotate_token(agent.member_id)
        token = session.get(Token, rotated.token_id)
        assert token is not None
        assert token.scopes == ["members.read"]


# ------------------------------------------ DEBT-37 / D-27: agent scope ceiling


def test_invite_accepts_the_propose_first_agent_default(engine: Engine) -> None:
    """The shipped My Agent default (reads + proposals.write/memory.write) mints."""
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        minted = service.invite(
            email="bot@team.dev",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write", "memory.read", "memory.write"],
        )
        token = session.get(Token, minted.token_id)
        assert token is not None
        assert set(token.scopes) == {
            "tickets.read",
            "proposals.write",
            "memory.read",
            "memory.write",
        }


@pytest.mark.parametrize(
    "over_scope",
    ["tickets.write", "memory.approve", "telemetry.write", "members.invite", "bogus.scope"],
)
def test_invite_refuses_an_over_scoped_agent(engine: Engine, over_scope: str) -> None:
    """An agent scope outside AGENT_SCOPE_CEILING — direct-write, approve, admin,
    or unknown — is refused at issuance, fail closed (DEBT-37 / D-27): the
    over-scoped agent that could self-approve or direct-write is unmintable."""
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        with pytest.raises(IdentityError, match="propose-first ceiling"):
            service.invite(
                email="overscoped@team.dev",
                role=Role.agent,
                scopes=["tickets.read", over_scope],
            )
        # Fail closed: nothing was created for the rejected invite.
        assert not any(m.email == "overscoped@team.dev" for m in service.list_members())


def test_rotate_heals_a_legacy_over_scoped_agent_token(engine: Engine) -> None:
    """A token minted before the clamp keeps no excess: rotation drops anything
    outside the ceiling (privilege only narrows)."""
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        agent = service.invite(email="bot@team.dev", role=Role.agent, scopes=["tickets.read"])
        # Simulate a legacy over-scoped token (issuance would now refuse this).
        legacy = session.get(Token, agent.token_id)
        assert legacy is not None
        legacy.scopes = ["tickets.read", "tickets.write", "proposals.write"]
        session.add(legacy)
        session.commit()

        rotated = service.rotate_token(agent.member_id)
        healed = session.get(Token, rotated.token_id)
        assert healed is not None
        assert "tickets.write" not in healed.scopes  # the excess is dropped
        assert set(healed.scopes) == {"tickets.read", "proposals.write"}


def test_revoke_member_kills_member_and_tokens(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        invited = service.invite(email="leaver@team.dev", role=Role.member)
        revoked = service.revoke_member(invited.member_id)
        assert revoked.status == "revoked"
    assert TokenVerifier(engine).verify(invited.plaintext) is None
    with Session(engine) as session:
        tokens = session.exec(select(Token).where(Token.member_id == invited.member_id)).all()
        assert all(t.revoked_at is not None for t in tokens)


def test_cannot_revoke_the_last_owner(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        owner = service.bootstrap_owner()
        assert owner is not None
        with pytest.raises(LastOwnerError):
            service.revoke_member(owner.member_id)


def test_can_revoke_an_owner_when_another_remains(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        first = service.bootstrap_owner()
        assert first is not None
        second = service.invite(email="co-owner@team.dev", role=Role.owner)
        revoked = service.revoke_member(first.member_id)
        assert revoked.status == "revoked"
        assert service.get_member(second.member_id).status == "invited"


def test_rotate_revoked_member_is_refused(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        invited = service.invite(email="leaver@team.dev", role=Role.member)
        service.revoke_member(invited.member_id)
        with pytest.raises(IdentityError, match="revoked"):
            service.rotate_token(invited.member_id)


def test_unknown_member_raises(engine: Engine) -> None:
    with Session(engine) as session, pytest.raises(MemberNotFoundError):
        IdentityService(session).get_member("01UNKNOWNMEMBER00000000000")


def test_list_members_in_creation_order(engine: Engine) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        service.bootstrap_owner()
        service.invite(email="b@team.dev", role=Role.member)
        service.invite(email="c@team.dev", role=Role.viewer)
        emails = [m.email for m in service.list_members()]
        assert emails == ["owner@local", "b@team.dev", "c@team.dev"]


def test_no_plaintext_token_at_rest(engine: Engine) -> None:
    """Sprint rule: tokens hashed at rest — nothing replayable in the DB."""
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    with Session(engine) as session:
        rows = session.exec(select(Token)).all()
        members = session.exec(select(Member)).all()
    blob = " ".join(
        [t.hashed for t in rows] + [str(t.scopes) for t in rows] + [m.email for m in members]
    )
    secret = minted.plaintext.split(".", 1)[1]
    assert secret not in blob
    assert all(t.hashed.startswith("$argon2id$") for t in rows)
