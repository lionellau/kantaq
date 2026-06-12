"""Telemetry API over HTTP: toggle, inspection, capture wiring (E28, MOD-25).

Pins the FR-E28 surface: default off, Maintainer+ flips the toggle (audited),
every human role can inspect, and the capture sites (proposal decisions, the
proposals list, the activity feed) record only registered numeric/categorical
props — proven against a sentinel-laden ticket.
"""

from __future__ import annotations

import json
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
from kantaq_db.models import AuditEvent, TelemetryEvent
from kantaq_mcp.tools import agent_action_propose
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock

SENTINEL = "TOPSECRET-payroll-Q3-runway"


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


@pytest.fixture
def viewer_token(engine: Engine, owner_token: str) -> str:
    with Session(engine) as session:
        return (
            IdentityService(session).invite(email="viewer@example.com", role=Role.viewer).plaintext
        )


@pytest.fixture
def agent(engine: Engine, owner_token: str) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(
            email="agent@example.com",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _enable(client: TestClient, token: str) -> None:
    response = client.put("/v1/telemetry", json={"enabled": True}, headers=_bearer(token))
    assert response.status_code == 200, response.text


def _create_ticket(client: TestClient, token: str, **overrides: Any) -> dict[str, Any]:
    project = client.post("/v1/projects", json={"name": "Proj"}, headers=_bearer(token))
    assert project.status_code == 201, project.text
    payload = {"project_id": project.json()["id"], "title": "A ticket", **overrides}
    response = client.post("/v1/tickets", json=payload, headers=_bearer(token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _propose(engine: Engine, agent_id: str, ticket_id: str, changes: dict[str, Any]) -> str:
    with Session(engine) as session:
        result = agent_action_propose(
            session,
            actor_id=agent_id,
            args={"ticket_id": ticket_id, "changes": changes, "note": ""},
            now=lambda: datetime.now(UTC).replace(tzinfo=None),
        )
    proposal_id: str = result["proposal"]["id"]
    return proposal_id


# -------------------------------------------------------------------- authz


def test_telemetry_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/telemetry").status_code == 401
    assert client.put("/v1/telemetry", json={"enabled": True}).status_code == 401


def test_every_human_role_may_inspect(client: TestClient, viewer_token: str) -> None:
    response = client.get("/v1/telemetry", headers=_bearer(viewer_token))
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_viewer_may_not_flip_the_toggle(client: TestClient, viewer_token: str) -> None:
    response = client.put("/v1/telemetry", json={"enabled": True}, headers=_bearer(viewer_token))
    assert response.status_code == 403


def test_agent_scope_does_not_include_telemetry(client: TestClient, agent: tuple[str, str]) -> None:
    _, token = agent
    assert client.get("/v1/telemetry", headers=_bearer(token)).status_code == 403


def test_even_a_telemetry_scoped_agent_cannot_flip_the_toggle(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    # SEC second review: the opt-in is a human privacy decision — an Agent
    # token minted with telemetry scopes must still be refused on PUT.
    with Session(engine) as session:
        minted = IdentityService(session).invite(
            email="scoped-bot@example.com",
            role=Role.agent,
            scopes=["telemetry.read", "telemetry.write"],
        )
    response = client.put(
        "/v1/telemetry", json={"enabled": True}, headers=_bearer(minted.plaintext)
    )
    assert response.status_code == 403
    assert client.get("/v1/telemetry", headers=_bearer(owner_token)).json()["enabled"] is False


# ------------------------------------------------------------------- toggle


def test_default_off_then_owner_enables_with_audit(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    assert client.get("/v1/telemetry", headers=_bearer(owner_token)).json()["enabled"] is False
    response = client.put("/v1/telemetry", json={"enabled": True}, headers=_bearer(owner_token))
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    with Session(engine) as session:
        actions = [r.action for r in session.exec(select(AuditEvent)).all()]
    assert "telemetry.enable" in actions


# ------------------------------------------------------------ capture wiring


def test_proposal_decision_records_outcome_events(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    _enable(client, owner_token)
    agent_id, _ = agent
    ticket = _create_ticket(client, owner_token, title=SENTINEL, description=SENTINEL)
    approved = _propose(engine, agent_id, ticket["id"], {"status": "doing"})
    rejected = _propose(engine, agent_id, ticket["id"], {"status": "done"})

    assert (
        client.post(f"/v1/proposals/{approved}/approve", headers=_bearer(owner_token)).status_code
        == 200
    )
    assert (
        client.post(f"/v1/proposals/{rejected}/reject", headers=_bearer(owner_token)).status_code
        == 200
    )

    body = client.get("/v1/telemetry", headers=_bearer(owner_token)).json()
    names = [event["name"] for event in body["events"]]
    assert "proposal_approved" in names
    assert "proposal_rejected" in names
    assert body["metrics"]["proposal_acceptance_rate"] == pytest.approx(0.5)
    assert body["metrics"]["median_seconds_to_approve"] is not None


def test_listing_proposals_and_activity_record_counts(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    _enable(client, owner_token)
    ticket = _create_ticket(client, owner_token)
    assert client.get("/v1/proposals", headers=_bearer(owner_token)).status_code == 200
    assert (
        client.get(f"/v1/tickets/{ticket['id']}/activity", headers=_bearer(owner_token)).status_code
        == 200
    )
    names = {
        event["name"]
        for event in client.get("/v1/telemetry", headers=_bearer(owner_token)).json()["events"]
    }
    assert {"proposals_listed", "activity_viewed"} <= names


def test_nothing_is_captured_while_opted_out(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    ticket = _create_ticket(client, owner_token)
    client.get("/v1/proposals", headers=_bearer(owner_token))
    client.get(f"/v1/tickets/{ticket['id']}/activity", headers=_bearer(owner_token))
    with Session(engine) as session:
        assert session.exec(select(TelemetryEvent)).all() == []


def test_sentinel_content_never_reaches_telemetry(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    """FR-E28-1 end to end: full capture over sentinel-laden domain data."""
    _enable(client, owner_token)
    agent_id, _ = agent
    ticket = _create_ticket(client, owner_token, title=SENTINEL, description=f"{SENTINEL} body")
    proposal = _propose(engine, agent_id, ticket["id"], {"status": "doing"})
    client.post(f"/v1/proposals/{proposal}/approve", headers=_bearer(owner_token))
    client.get("/v1/proposals", headers=_bearer(owner_token))
    client.get(f"/v1/tickets/{ticket['id']}/activity", headers=_bearer(owner_token))

    with Session(engine) as session:
        rows = session.exec(select(TelemetryEvent)).all()
        assert rows, "capture should have recorded events"
        dump = json.dumps([{"name": r.name, "props": r.props} for r in rows])
    assert SENTINEL not in dump
