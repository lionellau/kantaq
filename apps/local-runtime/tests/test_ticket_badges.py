"""The E19 per-row badges on the ticket API (MOD-11's server half).

``sync_state`` reads the event log: "draft" while any of the ticket's events
awaits a backend commit, "committed" once all are acked. ``pending_proposals``
counts only pending agent proposals.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_db.models import EventLog
from kantaq_mcp.tools import agent_action_propose
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def app(engine: Engine, tmp_path: Path) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))
    return create_app(settings=settings, engine=engine, verifier=verifier)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner_token(engine: Engine) -> str:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_ticket(client: TestClient, token: str, **overrides: Any) -> dict[str, Any]:
    project = client.post("/v1/projects", json={"name": "Proj"}, headers=_bearer(token))
    assert project.status_code == 201, project.text
    payload = {"project_id": project.json()["id"], "title": "A ticket", **overrides}
    response = client.post("/v1/tickets", json=payload, headers=_bearer(token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _commit_all_events(engine: Engine) -> None:
    """Simulate the backend acking every pending event (what push does)."""
    with Session(engine) as session:
        rows = session.exec(select(EventLog).where(EventLog.committed_rev == None)).all()  # noqa: E711
        for revision, row in enumerate(rows, start=1):
            row.committed_rev = revision
            session.add(row)
        session.commit()


def test_a_fresh_write_is_draft_until_the_backend_acks(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    ticket = _create_ticket(client, owner_token)
    assert ticket["sync_state"] == "draft"
    assert ticket["pending_proposals"] == 0

    _commit_all_events(engine)
    fetched = client.get(f"/v1/tickets/{ticket['id']}", headers=_bearer(owner_token)).json()
    assert fetched["sync_state"] == "committed"

    # A later local edit flips it back to draft until the next push.
    patched = client.patch(
        f"/v1/tickets/{ticket['id']}", json={"status": "doing"}, headers=_bearer(owner_token)
    ).json()
    assert patched["sync_state"] == "draft"


def test_pending_proposals_counts_only_pending(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    ticket = _create_ticket(client, owner_token)
    with Session(engine) as session:
        agent = IdentityService(session).invite(
            email="agent@example.com",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
    now = lambda: datetime.now(UTC).replace(tzinfo=None)  # noqa: E731
    with Session(engine) as session:
        first = agent_action_propose(
            session,
            actor_id=agent.member_id,
            args={"ticket_id": ticket["id"], "changes": {"status": "doing"}},
            now=now,
        )
        agent_action_propose(
            session,
            actor_id=agent.member_id,
            args={"ticket_id": ticket["id"], "changes": {"priority": "high"}},
            now=now,
        )

    listed = client.get("/v1/tickets", headers=_bearer(owner_token)).json()
    row = next(t for t in listed if t["id"] == ticket["id"])
    assert row["pending_proposals"] == 2

    decided = client.post(
        f"/v1/proposals/{first['proposal']['id']}/reject", headers=_bearer(owner_token)
    )
    assert decided.status_code == 200
    fetched = client.get(f"/v1/tickets/{ticket['id']}", headers=_bearer(owner_token)).json()
    assert fetched["pending_proposals"] == 1
