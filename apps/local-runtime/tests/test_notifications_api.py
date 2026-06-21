"""The notifications config API + the decision→webhook e2e (E20-T8, SEC).

GET/PUT /v1/notifications is opt-in default-off, Maintainer-gated, and returns
the sink HOST only (never the secret URL). An agent or a Viewer cannot set it
(never widens permission). The e2e proves a configured sink fires on approve AND
reject with a content-free payload — via an injected client, so no socket opens.
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
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_mcp.tools import agent_action_propose
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock

SENTINEL = "SECRET-TICKET-BODY-do-not-leak"


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _RecordingClient:
    """A shared stand-in httpx.Client the app uses instead of opening a socket."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: float | None = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        return _FakeResponse(200)

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


@pytest.fixture
def recorder() -> _RecordingClient:
    return _RecordingClient()


@pytest.fixture
def app(engine: Engine, tmp_path: Path, recorder: _RecordingClient) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))
    application = create_app(settings=settings, engine=engine, verifier=verifier)
    # Inject the recording client so a dispatch POSTs to the recorder, not a socket.
    application.state.notification_client_factory = lambda: recorder
    return application


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
        return IdentityService(session).invite(email="v@example.com", role=Role.viewer).plaintext


@pytest.fixture
def agent(engine: Engine, owner_token: str) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(
            email="agent@example.com", role=Role.agent, scopes=["tickets.read", "proposals.write"]
        )
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_proposal(client: TestClient, engine: Engine, owner_token: str, agent_id: str) -> str:
    project = client.post("/v1/projects", json={"name": "Proj"}, headers=_bearer(owner_token))
    ticket = client.post(
        "/v1/tickets",
        json={"project_id": project.json()["id"], "title": "A ticket"},
        headers=_bearer(owner_token),
    )
    with Session(engine) as session:
        result = agent_action_propose(
            session,
            actor_id=agent_id,
            args={"ticket_id": ticket.json()["id"], "changes": {"status": "doing"}},
            now=lambda: datetime.now(UTC).replace(tzinfo=None),
        )
    return str(result["proposal"]["id"])


# --------------------------------------------------------------- the config API


def test_get_is_default_off(client: TestClient, owner_token: str) -> None:
    body = client.get("/v1/notifications", headers=_bearer(owner_token)).json()
    assert body == {
        "enabled": False,
        "sink_type": "webhook",
        "sink_host": None,
        "configured": False,
    }


def test_put_sets_the_sink_and_get_returns_host_only(client: TestClient, owner_token: str) -> None:
    put = client.put(
        "/v1/notifications",
        json={
            "enabled": True,
            "sink_type": "slack",
            "webhook_url": "https://hooks.slack.com/services/T/B/SECRET",
        },
        headers=_bearer(owner_token),
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["enabled"] is True and body["sink_type"] == "slack"
    assert body["sink_host"] == "hooks.slack.com" and body["configured"] is True
    # The secret path is never returned.
    assert "SECRET" not in put.text
    assert "SECRET" not in client.get("/v1/notifications", headers=_bearer(owner_token)).text


def test_a_viewer_cannot_configure(client: TestClient, viewer_token: str) -> None:
    resp = client.put(
        "/v1/notifications",
        json={"enabled": False, "sink_type": "webhook", "webhook_url": None},
        headers=_bearer(viewer_token),
    )
    assert resp.status_code == 403


def test_an_agent_cannot_configure(client: TestClient, agent: tuple[str, str]) -> None:
    """never widens permission: an agent token can never enable or redirect the sink."""
    _, agent_token = agent
    resp = client.put(
        "/v1/notifications",
        json={
            "enabled": True,
            "sink_type": "webhook",
            "webhook_url": "https://evil.example.com/x",
        },
        headers=_bearer(agent_token),
    )
    assert resp.status_code == 403


def test_enable_without_url_is_422(client: TestClient, owner_token: str) -> None:
    resp = client.put(
        "/v1/notifications",
        json={"enabled": True, "sink_type": "webhook", "webhook_url": None},
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 422


# ------------------------------------------------------------- decision → webhook


def _configure_sink(client: TestClient, owner_token: str) -> None:
    resp = client.put(
        "/v1/notifications",
        json={
            "enabled": True,
            "sink_type": "webhook",
            "webhook_url": "https://hooks.example.com/x",
        },
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 200, resp.text


def test_approve_fires_a_content_free_webhook(
    client: TestClient,
    engine: Engine,
    owner_token: str,
    agent: tuple[str, str],
    recorder: _RecordingClient,
) -> None:
    _configure_sink(client, owner_token)
    proposal_id = _seed_proposal(client, engine, owner_token, agent[0])

    resp = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert resp.status_code == 200, resp.text

    assert len(recorder.calls) == 1
    body = recorder.calls[0]["json"]
    assert set(body) == {"action", "ids", "actor", "deep_link"}
    assert body["action"] == "proposal.approved"
    assert SENTINEL not in str(body)


def test_reject_fires_a_content_free_webhook(
    client: TestClient,
    engine: Engine,
    owner_token: str,
    agent: tuple[str, str],
    recorder: _RecordingClient,
) -> None:
    _configure_sink(client, owner_token)
    proposal_id = _seed_proposal(client, engine, owner_token, agent[0])

    resp = client.post(
        f"/v1/proposals/{proposal_id}/reject",
        json={"reason": SENTINEL},  # the reason must NEVER ride the notification
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 200, resp.text

    assert len(recorder.calls) == 1
    body = recorder.calls[0]["json"]
    assert body["action"] == "proposal.rejected"
    assert SENTINEL not in str(body)  # the reject reason stayed in the audit trail


def test_no_webhook_when_notifications_are_off(
    client: TestClient,
    engine: Engine,
    owner_token: str,
    agent: tuple[str, str],
    recorder: _RecordingClient,
) -> None:
    # Default off: do not configure a sink.
    proposal_id = _seed_proposal(client, engine, owner_token, agent[0])
    resp = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert resp.status_code == 200
    assert recorder.calls == []


# --------------------------------------------------------- notify-approver (E20-T9)


def test_notify_approver_fires_a_content_free_pending_signal(
    client: TestClient,
    engine: Engine,
    owner_token: str,
    agent: tuple[str, str],
    recorder: _RecordingClient,
) -> None:
    _configure_sink(client, owner_token)
    proposal_id = _seed_proposal(client, engine, owner_token, agent[0])

    resp = client.post(f"/v1/proposals/{proposal_id}/notify", headers=_bearer(owner_token))
    assert resp.status_code == 204

    assert len(recorder.calls) == 1
    body = recorder.calls[0]["json"]
    assert set(body) == {"action", "ids", "actor", "deep_link"}
    assert body["action"] == "proposal.pending"
    assert SENTINEL not in str(body)


def test_notify_approver_is_human_only(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    # never widens permission: an agent proposes but can never nudge / trigger
    # outbound traffic, even with its read scope.
    proposal_id = _seed_proposal(client, engine, owner_token, agent[0])
    resp = client.post(f"/v1/proposals/{proposal_id}/notify", headers=_bearer(agent[1]))
    assert resp.status_code == 403


def test_notify_a_decided_proposal_is_409(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    proposal_id = _seed_proposal(client, engine, owner_token, agent[0])
    client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    resp = client.post(f"/v1/proposals/{proposal_id}/notify", headers=_bearer(owner_token))
    assert resp.status_code == 409
