"""Grants API over HTTP: issue, list, revoke, and the no-secret-leak pin (E06).

The runtime half of the v0.1 grant slice. Permission shape mirrors tokens:
self-service for your own role-derived grants, ``tokens.rotate`` to manage
another member's (incl. agents), and agents can never issue. The SEC pin:
no response and no schema ever carries the device private key.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
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
)
from kantaq_db.models import AuditEvent
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
    """Bootstraps the Owner AND the runtime device key: (member_id, token)."""
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
        assert minted is not None
        ensure_device(session, keychain_for(settings), member_id=minted.member_id)
        session.commit()
    return minted.member_id, minted.plaintext


@pytest.fixture
def member(engine: Engine, owner: tuple[str, str]) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(email="m@example.com", role=Role.member)
    return minted.member_id, minted.plaintext


@pytest.fixture
def agent(engine: Engine, owner: tuple[str, str]) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(
            email="bot@example.com",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -------------------------------------------------------------------- authz


def test_grants_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/grants").status_code == 401
    assert client.post("/v1/grants", json={"resource": "w", "verbs": ["x"]}).status_code == 401


def test_a_member_issues_a_grant_for_themselves(
    client: TestClient, member: tuple[str, str]
) -> None:
    member_id, token = member
    response = client.post(
        "/v1/grants",
        json={"resource": "workspace/main", "verbs": ["tickets.read", "tickets.write"]},
        headers=_bearer(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subject"] == member_id
    assert body["valid"] is True
    assert body["reason"] == "ok"
    assert body["expires_at"] - body["issued_at"] == 3600  # the 1 h default


def test_a_member_cannot_issue_for_someone_else(
    client: TestClient, member: tuple[str, str], owner: tuple[str, str]
) -> None:
    owner_id, _ = owner
    _, token = member
    response = client.post(
        "/v1/grants",
        json={"resource": "workspace/main", "verbs": ["tickets.read"], "member_id": owner_id},
        headers=_bearer(token),
    )
    assert response.status_code == 403
    assert "tokens.rotate" in response.json()["detail"]


def test_a_maintainer_issues_an_agent_grant(
    client: TestClient, engine: Engine, owner: tuple[str, str], agent: tuple[str, str]
) -> None:
    _, owner_token = owner
    agent_id, _ = agent
    response = client.post(
        "/v1/grants",
        json={
            "resource": "workspace/main",
            "verbs": ["tickets.read", "proposals.write"],
            "member_id": agent_id,
            "ttl_seconds": 7200,
        },
        headers=_bearer(owner_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subject"] == agent_id
    assert body["expires_at"] - body["issued_at"] == 7200
    with Session(engine) as session:
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
    assert "grant.issue" in actions


def test_an_agent_can_never_issue_grants(client: TestClient, agent: tuple[str, str]) -> None:
    _, token = agent
    response = client.post(
        "/v1/grants",
        json={"resource": "workspace/main", "verbs": ["tickets.read"]},
        headers=_bearer(token),
    )
    assert response.status_code == 403
    assert "agents may not issue" in response.json()["detail"]


def test_role_widening_is_refused_at_the_api(
    client: TestClient, owner: tuple[str, str], engine: Engine
) -> None:
    _, owner_token = owner
    with Session(engine) as session:
        viewer = IdentityService(session).invite(email="v@example.com", role=Role.viewer)
    response = client.post(
        "/v1/grants",
        json={
            "resource": "workspace/main",
            "verbs": ["tickets.write"],
            "member_id": viewer.member_id,
        },
        headers=_bearer(owner_token),
    )
    assert response.status_code == 403
    assert "may not be granted" in response.json()["detail"]


def test_ttl_above_24h_is_refused(client: TestClient, member: tuple[str, str]) -> None:
    _, token = member
    response = client.post(
        "/v1/grants",
        json={"resource": "w/1", "verbs": ["tickets.read"], "ttl_seconds": 86_401},
        headers=_bearer(token),
    )
    assert response.status_code == 403
    assert "ceiling" in response.json()["detail"]


# ------------------------------------------------------------- list + revoke


def test_listing_defaults_to_own_grants(client: TestClient, member: tuple[str, str]) -> None:
    member_id, token = member
    client.post(
        "/v1/grants",
        json={"resource": "w/1", "verbs": ["tickets.read"]},
        headers=_bearer(token),
    )
    listed = client.get("/v1/grants", headers=_bearer(token))
    assert listed.status_code == 200
    assert [g["subject"] for g in listed.json()] == [member_id]


def test_self_revoke_and_admin_revoke(
    client: TestClient, owner: tuple[str, str], member: tuple[str, str]
) -> None:
    _, owner_token = owner
    _, member_token = member
    grant = client.post(
        "/v1/grants",
        json={"resource": "w/1", "verbs": ["tickets.read"]},
        headers=_bearer(member_token),
    ).json()

    revoked = client.post(f"/v1/grants/{grant['id']}/revoke", headers=_bearer(member_token))
    assert revoked.status_code == 200
    assert revoked.json()["valid"] is False
    assert revoked.json()["reason"] == "revoked"

    # Admin revokes another member's grant via tokens.rotate.
    second = client.post(
        "/v1/grants",
        json={"resource": "w/1", "verbs": ["tickets.read"]},
        headers=_bearer(member_token),
    ).json()
    by_admin = client.post(f"/v1/grants/{second['id']}/revoke", headers=_bearer(owner_token))
    assert by_admin.status_code == 200
    assert by_admin.json()["reason"] == "revoked"


def test_member_cannot_revoke_anothers_grant(
    client: TestClient, owner: tuple[str, str], member: tuple[str, str]
) -> None:
    _, owner_token = owner
    _, member_token = member
    owners_grant = client.post(
        "/v1/grants",
        json={"resource": "w/1", "verbs": ["tickets.read"]},
        headers=_bearer(owner_token),
    ).json()
    response = client.post(f"/v1/grants/{owners_grant['id']}/revoke", headers=_bearer(member_token))
    assert response.status_code == 403


def test_unknown_grant_is_404(client: TestClient, owner: tuple[str, str]) -> None:
    _, token = owner
    assert client.post("/v1/grants/nope/revoke", headers=_bearer(token)).status_code == 404


# ------------------------------------------------------------ no-secret-leak


def test_no_response_or_schema_ever_carries_the_device_seed(
    client: TestClient, engine: Engine, settings: Settings, owner: tuple[str, str]
) -> None:
    """NFR-E06-1 extended to device keys (sprint exit criterion 3)."""
    _, token = owner
    seed = keychain_for(settings).get(DEVICE_KEY_NAME)
    assert seed is not None

    issued = client.post(
        "/v1/grants",
        json={"resource": "w/1", "verbs": ["tickets.read"]},
        headers=_bearer(token),
    )
    listed = client.get("/v1/grants", headers=_bearer(token))
    schema = client.get("/openapi.json")
    for response in (issued, listed, schema):
        assert seed not in response.text

    # And the row store itself: only the verify key is registered.
    with Session(engine) as session:
        from kantaq_db.models import Device

        dump = json.dumps([row.model_dump(mode="json") for row in session.exec(select(Device))])
    assert seed not in dump


def test_listing_another_members_grants_needs_tokens_rotate(
    client: TestClient, owner: tuple[str, str], member: tuple[str, str]
) -> None:
    """E27 review: no cross-member grant enumeration for plain members."""
    owner_id, owner_token = owner
    _, member_token = member
    denied = client.get(f"/v1/grants?member={owner_id}", headers=_bearer(member_token))
    assert denied.status_code == 403
    allowed = client.get(f"/v1/grants?member={member[0]}", headers=_bearer(owner_token))
    assert allowed.status_code == 200
