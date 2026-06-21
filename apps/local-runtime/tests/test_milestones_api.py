"""Milestone API over HTTP (E14-T3): CRUD, the Viewer/Member role matrix, the
ticket-membership routes, and the backlog milestone badge proven batched (no
N+1 at scale, NFR-E12-1 discipline)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, Role, TokenVerifier
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


@pytest.fixture
def viewer_token(engine: Engine, owner_token: str) -> str:
    with Session(engine) as session:
        return (
            IdentityService(session).invite(email="viewer@example.com", role=Role.viewer).plaintext
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _project(client: TestClient, token: str, **overrides: Any) -> str:
    r = client.post("/v1/projects", json={"name": "Proj", **overrides}, headers=_bearer(token))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _ticket(client: TestClient, token: str, project_id: str, **overrides: Any) -> str:
    payload = {"project_id": project_id, "title": "A ticket", **overrides}
    r = client.post("/v1/tickets", json=payload, headers=_bearer(token))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _milestone(client: TestClient, token: str, project_id: str, **overrides: Any) -> dict[str, Any]:
    payload = {"project_id": project_id, "name": "v1.0", **overrides}
    r = client.post("/v1/milestones", json=payload, headers=_bearer(token))
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


# -------------------------------------------------------------------- authz


def test_milestone_routes_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/milestones").status_code == 401
    assert client.post("/v1/milestones", json={"project_id": "x", "name": "y"}).status_code == 401


def test_viewer_reads_but_cannot_write_milestones(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    project = _project(client, owner_token)
    milestone = _milestone(client, owner_token, project)

    listed = client.get("/v1/milestones", headers=_bearer(viewer_token))
    assert listed.status_code == 200
    assert [m["id"] for m in listed.json()] == [milestone["id"]]

    denied = client.post(
        "/v1/milestones",
        json={"project_id": project, "name": "nope"},
        headers=_bearer(viewer_token),
    )
    assert denied.status_code == 403

    patched = client.patch(
        f"/v1/milestones/{milestone['id']}",
        json={"status": "complete"},
        headers=_bearer(viewer_token),
    )
    assert patched.status_code == 403

    deleted = client.delete(f"/v1/milestones/{milestone['id']}", headers=_bearer(viewer_token))
    assert deleted.status_code == 403


# -------------------------------------------------------------- round-trips


def test_milestone_crud_round_trip(client: TestClient, owner_token: str) -> None:
    project = _project(client, owner_token)
    created = _milestone(
        client, owner_token, project, description="the launch", target_date="2026-09-01T00:00:00Z"
    )
    assert created["status"] == "active"
    assert created["ticket_count"] == 0

    fetched = client.get(f"/v1/milestones/{created['id']}", headers=_bearer(owner_token))
    assert fetched.status_code == 200
    assert fetched.json()["description"] == "the launch"

    patched = client.patch(
        f"/v1/milestones/{created['id']}",
        json={"status": "complete", "name": "v1.0 (shipped)"},
        headers=_bearer(owner_token),
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "complete"
    assert patched.json()["name"] == "v1.0 (shipped)"

    listed = client.get(
        "/v1/milestones", params={"project_id": project}, headers=_bearer(owner_token)
    )
    assert [m["id"] for m in listed.json()] == [created["id"]]


def test_bad_status_is_422(client: TestClient, owner_token: str) -> None:
    project = _project(client, owner_token)
    r = client.post(
        "/v1/milestones",
        json={"project_id": project, "name": "m", "status": "shipped"},
        headers=_bearer(owner_token),
    )
    assert r.status_code == 422


# --------------------------------------------------------- ticket membership


def test_add_and_remove_ticket_membership_moves_the_badge(
    client: TestClient, owner_token: str
) -> None:
    project = _project(client, owner_token)
    ticket = _ticket(client, owner_token, project)
    milestone = _milestone(client, owner_token, project)

    added = client.post(
        f"/v1/milestones/{milestone['id']}/tickets",
        json={"ticket_id": ticket},
        headers=_bearer(owner_token),
    )
    assert added.status_code == 204

    # The milestone now reports one ticket; the ticket carries the badge.
    m = client.get(f"/v1/milestones/{milestone['id']}", headers=_bearer(owner_token)).json()
    assert m["ticket_count"] == 1
    listed = client.get("/v1/tickets", headers=_bearer(owner_token)).json()
    assert next(t for t in listed if t["id"] == ticket)["milestone_count"] == 1

    removed = client.delete(
        f"/v1/milestones/{milestone['id']}/tickets/{ticket}", headers=_bearer(owner_token)
    )
    assert removed.status_code == 204
    listed = client.get("/v1/tickets", headers=_bearer(owner_token)).json()
    assert next(t for t in listed if t["id"] == ticket)["milestone_count"] == 0


def test_cross_project_membership_is_422(client: TestClient, owner_token: str) -> None:
    project_a = _project(client, owner_token)
    project_b = _project(client, owner_token, name="Other")
    milestone = _milestone(client, owner_token, project_a)
    foreign = _ticket(client, owner_token, project_b)
    r = client.post(
        f"/v1/milestones/{milestone['id']}/tickets",
        json={"ticket_id": foreign},
        headers=_bearer(owner_token),
    )
    assert r.status_code == 422


def test_duplicate_membership_is_422(client: TestClient, owner_token: str) -> None:
    project = _project(client, owner_token)
    ticket = _ticket(client, owner_token, project)
    milestone = _milestone(client, owner_token, project)
    url = f"/v1/milestones/{milestone['id']}/tickets"
    assert (
        client.post(url, json={"ticket_id": ticket}, headers=_bearer(owner_token)).status_code
        == 204
    )
    assert (
        client.post(url, json={"ticket_id": ticket}, headers=_bearer(owner_token)).status_code
        == 422
    )


# ----------------------------------------------------- the badge is batched


def test_milestone_badge_is_one_query_regardless_of_ticket_count(
    client: TestClient, owner_token: str, engine: Engine
) -> None:
    """The backlog milestone badge is computed in a single batched query — the
    ``ticket_milestones`` SELECT count for GET /v1/tickets is exactly one no
    matter how many tickets carry the badge (no N+1, NFR-E12-1 discipline)."""
    project = _project(client, owner_token)
    milestone = _milestone(client, owner_token, project)
    for _ in range(8):
        t = _ticket(client, owner_token, project)
        client.post(
            f"/v1/milestones/{milestone['id']}/tickets",
            json={"ticket_id": t},
            headers=_bearer(owner_token),
        )

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    try:
        listed = client.get("/v1/tickets", headers=_bearer(owner_token)).json()
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(listed) == 8
    assert all(t["milestone_count"] == 1 for t in listed)
    # Exactly one read of the junction for the whole page — not one per ticket.
    membership_reads = [
        s
        for s in statements
        if "ticket_milestones" in s and s.lstrip().upper().startswith("SELECT")
    ]
    assert len(membership_reads) == 1, f"expected 1 batched read, got {len(membership_reads)}"
