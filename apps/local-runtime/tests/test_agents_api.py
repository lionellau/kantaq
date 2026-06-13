"""Agents API over HTTP: the live agent-session trust surface (E20-T3, MOD-12, SEC).

A session is a capability grant whose subject is an Agent-role member. The page
must be honest and complete (NFR-E20-1): every agent grant is listed, ``active``
reflects the *live* grant state with no cache, human self-grants are excluded,
and cross-member enumeration is gated by ``tokens.rotate``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, Role, TokenVerifier, ensure_device
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


def _issue(client: TestClient, owner_token: str, agent_id: str, verbs: list[str]) -> dict:
    response = client.post(
        "/v1/grants",
        json={"resource": "workspace/main", "verbs": verbs, "member_id": agent_id},
        headers=_bearer(owner_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_sessions_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/agents/sessions").status_code == 401


def test_lists_an_agent_session_with_owner_and_derived_write_mode(
    client: TestClient, owner: tuple[str, str], agent: tuple[str, str]
) -> None:
    _, owner_token = owner
    agent_id, _ = agent
    grant = _issue(client, owner_token, agent_id, ["tickets.read", "proposals.write"])

    listed = client.get("/v1/agents/sessions", headers=_bearer(owner_token))
    assert listed.status_code == 200, listed.text
    sessions = listed.json()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["grant_id"] == grant["id"]
    assert session["owner_member_id"] == agent_id
    assert session["owner_email"] == "bot@example.com"
    assert session["owner_role"] == "Agent"
    assert session["resource"] == "workspace/main"
    assert sorted(session["verbs"]) == ["proposals.write", "tickets.read"]
    assert session["write_mode"] == "propose_only"  # carries proposals.write
    assert session["active"] is True
    assert session["reason"] == "ok"


def test_read_only_write_mode_when_no_propose_verb(
    client: TestClient, owner: tuple[str, str], agent: tuple[str, str]
) -> None:
    _, owner_token = owner
    agent_id, _ = agent
    _issue(client, owner_token, agent_id, ["tickets.read"])
    sessions = client.get("/v1/agents/sessions", headers=_bearer(owner_token)).json()
    assert sessions[0]["write_mode"] == "read_only"


def test_human_self_grants_are_not_agent_sessions(
    client: TestClient, owner: tuple[str, str], member: tuple[str, str], agent: tuple[str, str]
) -> None:
    """A page padded with signing self-grants would be dishonest: only agents."""
    _, owner_token = owner
    member_id, member_token = member
    agent_id, _ = agent
    # A human member's own grant (the kind ensure_member_grant makes for signing).
    client.post(
        "/v1/grants",
        json={"resource": "workspace/main", "verbs": ["tickets.read"]},
        headers=_bearer(member_token),
    )
    _issue(client, owner_token, agent_id, ["tickets.read"])

    sessions = client.get("/v1/agents/sessions", headers=_bearer(owner_token)).json()
    assert [s["owner_member_id"] for s in sessions] == [agent_id]


def test_active_flips_false_the_instant_a_grant_is_revoked(
    client: TestClient, owner: tuple[str, str], agent: tuple[str, str]
) -> None:
    """NFR-E20-1: no cache — a revoked session reads inactive on the next call."""
    _, owner_token = owner
    agent_id, _ = agent
    grant = _issue(client, owner_token, agent_id, ["tickets.read"])

    assert client.get("/v1/agents/sessions", headers=_bearer(owner_token)).json()[0]["active"]
    client.post(f"/v1/grants/{grant['id']}/revoke", headers=_bearer(owner_token))
    after = client.get("/v1/agents/sessions", headers=_bearer(owner_token)).json()[0]
    assert after["active"] is False
    assert after["reason"] == "revoked"
    assert after["revoked_at"] is not None


def test_plain_member_cannot_enumerate_the_workspace(
    client: TestClient, owner: tuple[str, str], member: tuple[str, str], agent: tuple[str, str]
) -> None:
    """Without tokens.rotate a member sees no one else's agents (E27 boundary)."""
    _, owner_token = owner
    _, member_token = member
    agent_id, _ = agent
    _issue(client, owner_token, agent_id, ["tickets.read"])

    listed = client.get("/v1/agents/sessions", headers=_bearer(member_token))
    assert listed.status_code == 200
    assert listed.json() == []  # the member subjects no agent grants
