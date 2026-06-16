"""Invitations API over HTTP (E06-T8, DEBT-04): craft + accept the twp://invite.

The protocol-correct onboarding end to end: a Maintainer crafts a device-signed
invite, the invitee's runtime accepts it — and a forged, expired, or unknown-root
invite is refused with its reason. The signed-bundle crypto itself is pinned in
``packages/protocol/tests/test_invites.py``; this proves the runtime wiring +
the verify-against-the-device-root gate + idempotent admission.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import (
    DEVICE_KEY_NAME,
    IdentityService,
    Role,
    TokenVerifier,
    ensure_device,
    local_device,
)
from kantaq_db.models import Member
from kantaq_protocol import (
    Invite,
    decode_invite_uri,
    encode_invite_uri,
    generate_keypair,
    sign_invite,
)
from kantaq_runtime.app import create_app
from kantaq_runtime.auth import keychain_for
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))


@pytest.fixture
def app(engine: Engine, settings: Settings) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    return create_app(settings=settings, engine=engine, verifier=verifier)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner(engine: Engine, settings: Settings) -> tuple[str, str]:
    """Bootstraps the Owner + the runtime device key (the invite issuer root)."""
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
        assert minted is not None
        ensure_device(session, keychain_for(settings), member_id=minted.member_id)
        session.commit()
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _craft(client: TestClient, owner_token: str, **over: object) -> dict:
    body: dict[str, object] = {"email": "newbie@acme.dev", "role": "Member"}
    body.update(over)
    response = client.post("/v1/invitations", json=body, headers=_bearer(owner_token))
    assert response.status_code == 201, response.text
    return response.json()


def _device_root(engine: Engine, settings: Settings) -> tuple[str, str, str]:
    """(issuer device id, workspace id, device seed) for hand-crafting invites."""
    seed = keychain_for(settings).get(DEVICE_KEY_NAME)
    assert seed is not None
    with Session(engine) as session:
        device = local_device(session, keychain_for(settings))
        assert device is not None
        member = session.exec(select(Member)).first()
        assert member is not None
        return device.id, member.workspace_id, seed


# ------------------------------------------------------------------ happy path


def test_craft_then_accept_admits_the_member(client: TestClient, owner: tuple[str, str]) -> None:
    _, owner_token = owner
    crafted = _craft(client, owner_token)
    assert crafted["invite"].startswith("twp://invite/")

    accepted = client.post(
        "/v1/invitations/accept", json={"invite": crafted["invite"]}, headers=_bearer(owner_token)
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["email"] == "newbie@acme.dev"
    assert body["role"] == "Member"
    assert body["reused"] is False
    assert body["token"] is not None  # the new member's one-time local token


def test_accept_is_idempotent(client: TestClient, owner: tuple[str, str]) -> None:
    _, owner_token = owner
    invite = _craft(client, owner_token)["invite"]
    first = client.post(
        "/v1/invitations/accept", json={"invite": invite}, headers=_bearer(owner_token)
    )
    second = client.post(
        "/v1/invitations/accept", json={"invite": invite}, headers=_bearer(owner_token)
    )
    assert first.json()["reused"] is False
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["token"] is None  # no second admission, no new token


# ------------------------------------------------------- the verify-the-root gate


def test_a_forged_invite_is_rejected(client: TestClient, owner: tuple[str, str]) -> None:
    _, owner_token = owner
    invite_uri = _craft(client, owner_token)["invite"]
    # Widen the role after signing — the signature no longer covers it.
    tampered = encode_invite_uri(replace(decode_invite_uri(invite_uri), role="Owner"))
    response = client.post(
        "/v1/invitations/accept", json={"invite": tampered}, headers=_bearer(owner_token)
    )
    assert response.status_code == 400
    assert "forged" in response.json()["detail"]


def test_an_expired_invite_is_rejected(
    client: TestClient, engine: Engine, settings: Settings, owner: tuple[str, str]
) -> None:
    issuer_id, workspace_id, seed = _device_root(engine, settings)
    expired = sign_invite(
        Invite(
            invite_id="inv_expired00000000000001",
            workspace_id=workspace_id,
            subject_email="late@acme.dev",
            role="Member",
            resource=workspace_id,
            verbs=("tickets.read",),
            issuer=issuer_id,
            issued_at=1_000_000,
            expires_at=1_000_001,  # long past — expired against now
        ),
        seed,
    )
    response = client.post(
        "/v1/invitations/accept",
        json={"invite": encode_invite_uri(expired)},
        headers=_bearer(owner[1]),
    )
    assert response.status_code == 400
    assert "expired" in response.json()["detail"]


def test_an_unknown_root_invite_is_rejected(client: TestClient, owner: tuple[str, str]) -> None:
    """An invite signed by a device that is not a registered root is refused."""
    rogue_key = generate_keypair()
    rogue = sign_invite(
        Invite(
            invite_id="inv_rogue000000000000001",
            workspace_id="ws_x00000000000000000000",
            subject_email="evil@acme.dev",
            role="Owner",
            resource="ws_x00000000000000000000",
            verbs=("tickets.read",),
            issuer="dev_rogue00000000000000001",
            issued_at=1,
            expires_at=2_000_000_000,  # far future, so the only failure is the root
        ),
        rogue_key.private_key,
    )
    response = client.post(
        "/v1/invitations/accept",
        json={"invite": encode_invite_uri(rogue)},
        headers=_bearer(owner[1]),
    )
    assert response.status_code == 400
    assert "unknown_root" in response.json()["detail"]


def test_a_malformed_invite_is_rejected(client: TestClient, owner: tuple[str, str]) -> None:
    response = client.post(
        "/v1/invitations/accept",
        json={"invite": "twp://invite/!!!not-base64"},
        headers=_bearer(owner[1]),
    )
    assert response.status_code == 400
    assert "malformed" in response.json()["detail"]


# ------------------------------------------------------------------ authz


def test_accept_requires_a_token(client: TestClient) -> None:
    assert client.post("/v1/invitations/accept", json={"invite": "twp://invite/x"}).status_code == (
        401
    )


def test_crafting_needs_members_invite(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    with Session(engine) as session:
        member = IdentityService(session).invite(email="plain@acme.dev", role=Role.member)
    response = client.post(
        "/v1/invitations",
        json={"email": "x@acme.dev", "role": "Member"},
        headers=_bearer(member.plaintext),
    )
    assert response.status_code == 403


def test_agents_cannot_be_invited_by_uri(client: TestClient, owner: tuple[str, str]) -> None:
    _, owner_token = owner
    response = client.post(
        "/v1/invitations",
        json={"email": "bot@acme.dev", "role": "Agent"},
        headers=_bearer(owner_token),
    )
    assert response.status_code == 400
    assert "token scopes" in response.json()["detail"]


# ----------------------------------------------------- SEC-review hardening


def test_a_maintainer_cannot_craft_an_owner_invite(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    """SEC: members.invite is not enough to mint an Owner — only an Owner can,
    so a Maintainer cannot escalate by inviting an Owner ally."""
    with Session(engine) as session:
        maint = IdentityService(session).invite(email="maint@acme.dev", role=Role.maintainer)
    escalate = client.post(
        "/v1/invitations",
        json={"email": "ally@acme.dev", "role": "Owner"},
        headers=_bearer(maint.plaintext),
    )
    assert escalate.status_code == 403
    assert "Owner" in escalate.json()["detail"]
    # Positive controls: the Maintainer may invite a Member; the Owner may invite an Owner.
    assert (
        client.post(
            "/v1/invitations",
            json={"email": "newbie@acme.dev", "role": "Member"},
            headers=_bearer(maint.plaintext),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/invitations",
            json={"email": "coowner@acme.dev", "role": "Owner"},
            headers=_bearer(owner[1]),
        ).status_code
        == 201
    )


def test_a_directly_signed_agent_invite_is_refused_on_accept(
    client: TestClient, engine: Engine, settings: Settings, owner: tuple[str, str]
) -> None:
    """accept enforces the no-agent rule too — not just craft — so a hand-signed
    invite from a real device root cannot smuggle an Agent member in."""
    issuer_id, workspace_id, seed = _device_root(engine, settings)
    agent_invite = sign_invite(
        Invite(
            invite_id="inv_agent000000000000001",
            workspace_id=workspace_id,
            subject_email="bot@acme.dev",
            role="Agent",
            resource=workspace_id,
            verbs=("tickets.read",),
            issuer=issuer_id,
            issued_at=1,
            expires_at=2_000_000_000,
        ),
        seed,
    )
    response = client.post(
        "/v1/invitations/accept",
        json={"invite": encode_invite_uri(agent_invite)},
        headers=_bearer(owner[1]),
    )
    assert response.status_code == 400
    assert "token scopes" in response.json()["detail"]


def test_an_invite_for_another_workspace_is_refused(
    client: TestClient, engine: Engine, settings: Settings, owner: tuple[str, str]
) -> None:
    issuer_id, _, seed = _device_root(engine, settings)
    foreign = sign_invite(
        Invite(
            invite_id="inv_foreign00000000000001",
            workspace_id="ws_elsewhere0000000000001",
            subject_email="x@acme.dev",
            role="Member",
            resource="ws_elsewhere0000000000001",
            verbs=("tickets.read",),
            issuer=issuer_id,
            issued_at=1,
            expires_at=2_000_000_000,
        ),
        seed,
    )
    response = client.post(
        "/v1/invitations/accept",
        json={"invite": encode_invite_uri(foreign)},
        headers=_bearer(owner[1]),
    )
    assert response.status_code == 400
    assert "different workspace" in response.json()["detail"]


def test_a_revoked_member_cannot_be_readmitted_by_invite(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    _, owner_token = owner
    invite = _craft(client, owner_token)["invite"]
    admitted = client.post(
        "/v1/invitations/accept", json={"invite": invite}, headers=_bearer(owner_token)
    )
    member_id = admitted.json()["member_id"]
    with Session(engine) as session:
        IdentityService(session).revoke_member(member_id)
        session.commit()
    re_accept = client.post(
        "/v1/invitations/accept", json={"invite": invite}, headers=_bearer(owner_token)
    )
    assert re_accept.status_code == 409
    assert "revoked" in re_accept.json()["detail"]
