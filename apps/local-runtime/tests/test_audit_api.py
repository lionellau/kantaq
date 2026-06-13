"""Audit API over HTTP: the live audit-log read (E20-T3, MOD-12, SEC).

``GET /v1/audit/range`` feeds the Agents page and the Inbox denied-calls tab. It
reads the append-only log live (no cache, NFR-E20-1), lifts a denial's reason /
detail / session_id out of ``after``, never echoes raw before/after snapshots,
and gates cross-member reads behind ``tokens.rotate``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core import audit
from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_runtime.app import create_app
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
def owner(engine: Engine) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
        assert minted is not None
    return minted.member_id, minted.plaintext


@pytest.fixture
def member(engine: Engine, owner: tuple[str, str]) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(email="m@example.com", role=Role.member)
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def _deny(session: Session, actor_id: str, tool: str, *, reason: str, at: int) -> None:
    audit.write(
        session,
        actor_id=actor_id,
        action="tool.deny",
        source="mcp",
        object_ref=f"tools/{tool}",
        after={"reason": reason, "detail": f"{tool} blocked", "session_id": "sess-1"},
        now=_ts(at),
    )


def test_range_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/audit/range").status_code == 401


def test_returns_own_rows_newest_first(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    owner_id, token = owner
    with Session(engine) as session:
        _deny(session, owner_id, "ticket_search", reason="tool_allowlist", at=1)
        _deny(session, owner_id, "memory_get", reason="memory_policy", at=2)
        session.commit()

    rows = client.get("/v1/audit/range", headers=_bearer(token)).json()
    assert [r["object_ref"] for r in rows] == ["tools/memory_get", "tools/ticket_search"]
    assert rows[0]["reason"] == "memory_policy"
    assert rows[0]["detail"] == "memory_get blocked"
    assert rows[0]["session_id"] == "sess-1"


def test_action_filter_selects_denied_calls(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    owner_id, token = owner
    with Session(engine) as session:
        _deny(session, owner_id, "ticket_search", reason="tool_allowlist", at=1)
        audit.write(
            session,
            actor_id=owner_id,
            action="proposal.create",
            source="mcp",
            object_ref="agent_proposals/p1",
            after={"title": "ok"},
            now=_ts(2),
        )
        session.commit()

    denied = client.get(
        "/v1/audit/range", params={"action": "tool.deny", "source": "mcp"}, headers=_bearer(token)
    ).json()
    assert [r["action"] for r in denied] == ["tool.deny"]


def test_never_echoes_raw_before_after_snapshots(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    """A row's after can hold a full entity snapshot; the endpoint curates it out."""
    owner_id, token = owner
    with Session(engine) as session:
        audit.write(
            session,
            actor_id=owner_id,
            action="proposal.create",
            source="mcp",
            object_ref="agent_proposals/p1",
            after={"title": "TOP-SECRET-SNAPSHOT", "description": "leak-me"},
            now=_ts(1),
        )
        session.commit()

    response = client.get("/v1/audit/range", headers=_bearer(token))
    assert "TOP-SECRET-SNAPSHOT" not in response.text
    assert "leak-me" not in response.text
    row = response.json()[0]
    assert row["reason"] is None and row["detail"] is None  # non-deny rows carry none


def test_member_cannot_read_anothers_trail_without_tokens_rotate(
    client: TestClient, engine: Engine, owner: tuple[str, str], member: tuple[str, str]
) -> None:
    owner_id, owner_token = owner
    member_id, member_token = member
    with Session(engine) as session:
        _deny(session, owner_id, "ticket_search", reason="tool_allowlist", at=1)
        _deny(session, member_id, "memory_get", reason="memory_policy", at=2)
        session.commit()

    # The member, with no member= filter, sees only their own trail.
    own = client.get("/v1/audit/range", headers=_bearer(member_token)).json()
    assert {r["actor_id"] for r in own} == {member_id}

    # Asking for the owner's trail is refused.
    denied = client.get(
        "/v1/audit/range", params={"member": owner_id}, headers=_bearer(member_token)
    )
    assert denied.status_code == 403

    # The owner (tokens.rotate) sees the whole workspace by default.
    allall = client.get("/v1/audit/range", headers=_bearer(owner_token)).json()
    assert {r["actor_id"] for r in allall} == {owner_id, member_id}


def test_limit_is_capped(client: TestClient, owner: tuple[str, str]) -> None:
    _, token = owner
    assert client.get("/v1/audit/range?limit=500", headers=_bearer(token)).status_code == 422
    assert client.get("/v1/audit/range?limit=0", headers=_bearer(token)).status_code == 422


def test_malformed_filters_are_rejected(client: TestClient, owner: tuple[str, str]) -> None:
    _, token = owner
    assert client.get("/v1/audit/range?source=bogus", headers=_bearer(token)).status_code == 422
    long_action = "a" * 65
    assert (
        client.get(f"/v1/audit/range?action={long_action}", headers=_bearer(token)).status_code
        == 422
    )
