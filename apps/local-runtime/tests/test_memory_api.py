"""Memory API over HTTP (E13-T2): round-trips, links, authz, privacy shape.

Pins the role matrix at the memory surface (Viewer reads but cannot write, no
token is 401), the REST face of the link integrity rules, and that the API
exposes the computed ``domain_visibility`` label from the one mapping table.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_db.models import EventLog, MemoryLink
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


@pytest.fixture
def agent_token(engine: Engine, owner_token: str) -> str:
    """An Agent token with the memory read/write scopes — but NOT memory.approve.

    An agent may propose (memory.write) yet must never approve (E13-T4): the
    scope it cannot hold is the propose-first guard, mirroring proposals.
    """
    with Session(engine) as session:
        return (
            IdentityService(session)
            .invite(
                email="agent@example.com",
                role=Role.agent,
                scopes=["memory.read", "memory.write"],
            )
            .plaintext
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_ticket(client: TestClient, token: str) -> str:
    project = client.post("/v1/projects", json={"name": "P"}, headers=_bearer(token))
    assert project.status_code == 201, project.text
    ticket = client.post(
        "/v1/tickets",
        json={"project_id": project.json()["id"], "title": "T"},
        headers=_bearer(token),
    )
    assert ticket.status_code == 201, ticket.text
    ticket_id: str = ticket.json()["id"]
    return ticket_id


def _create_memory(client: TestClient, token: str, **overrides: Any) -> dict[str, Any]:
    payload = {"title": "A memory", **overrides}
    response = client.post("/v1/memory", json=payload, headers=_bearer(token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# -------------------------------------------------------------------- authz


def test_memory_routes_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/memory").status_code == 401
    assert client.post("/v1/memory", json={"title": "X"}).status_code == 401


def test_viewer_reads_but_cannot_write(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    created = _create_memory(client, owner_token)

    listed = client.get("/v1/memory", headers=_bearer(viewer_token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [created["id"]]

    denied = client.post("/v1/memory", json={"title": "nope"}, headers=_bearer(viewer_token))
    assert denied.status_code == 403
    patched = client.patch(
        f"/v1/memory/{created['id']}", json={"body": "x"}, headers=_bearer(viewer_token)
    )
    assert patched.status_code == 403
    deleted = client.delete(f"/v1/memory/{created['id']}", headers=_bearer(viewer_token))
    assert deleted.status_code == 403


# -------------------------------------------------------------------- entries


def test_create_get_update_delete_round_trip(client: TestClient, owner_token: str) -> None:
    created = _create_memory(
        client,
        owner_token,
        body="why we sync this way",
        type="decision",
        space="codebase",
        confidence="high",
    )
    assert created["type"] == "decision"
    assert created["domain_visibility"] == "personal_synced"
    assert created["provenance"]["origin"] == "manual"

    fetched = client.get(f"/v1/memory/{created['id']}", headers=_bearer(owner_token))
    assert fetched.status_code == 200
    assert fetched.json()["body"] == "why we sync this way"

    patched = client.patch(
        f"/v1/memory/{created['id']}",
        json={"review_status": "stale"},
        headers=_bearer(owner_token),
    )
    assert patched.status_code == 200
    assert patched.json()["review_status"] == "stale"

    deleted = client.delete(f"/v1/memory/{created['id']}", headers=_bearer(owner_token))
    assert deleted.status_code == 204
    assert client.get(f"/v1/memory/{created['id']}", headers=_bearer(owner_token)).status_code == (
        404
    )


def test_local_entry_shape_and_immutability(client: TestClient, owner_token: str) -> None:
    created = _create_memory(client, owner_token, title="private", visibility="local")
    assert created["visibility"] == "local"
    assert created["domain_visibility"] == "private_local"

    # The PATCH shape forbids visibility outright (extra="forbid").
    rejected = client.patch(
        f"/v1/memory/{created['id']}",
        json={"visibility": "team"},
        headers=_bearer(owner_token),
    )
    assert rejected.status_code == 422


def test_validation_maps_to_422(client: TestClient, owner_token: str) -> None:
    bad_vocab = client.post(
        "/v1/memory", json={"title": "T", "space": "bogus"}, headers=_bearer(owner_token)
    )
    assert bad_vocab.status_code == 422
    assert "unknown space" in bad_vocab.json()["detail"]

    created = _create_memory(client, owner_token)
    blocked = client.patch(
        f"/v1/memory/{created['id']}",
        json={"review_status": "approved"},
        headers=_bearer(owner_token),
    )
    assert blocked.status_code == 422
    assert "promotion workflow" in blocked.json()["detail"]


def test_list_filters_and_search(client: TestClient, owner_token: str) -> None:
    _create_memory(client, owner_token, title="Sync design", type="decision")
    _create_memory(client, owner_token, title="Auth note", body="JWT only")

    by_type = client.get("/v1/memory?type=decision", headers=_bearer(owner_token))
    assert [row["title"] for row in by_type.json()] == ["Sync design"]

    by_q = client.get("/v1/memory?q=jwt", headers=_bearer(owner_token))
    assert [row["title"] for row in by_q.json()] == ["Auth note"]


# ---------------------------------------------------------------------- links


def test_link_and_ticket_memory_round_trip(client: TestClient, owner_token: str) -> None:
    ticket_id = _create_ticket(client, owner_token)
    created = _create_memory(client, owner_token, title="Context", type="constraint")

    linked = client.post(
        f"/v1/memory/{created['id']}/link",
        json={"ticket_id": ticket_id, "reason": "explains the constraint"},
        headers=_bearer(owner_token),
    )
    assert linked.status_code == 201, linked.text
    assert linked.json()["visibility"] == "team"

    links = client.get(f"/v1/memory/{created['id']}/links", headers=_bearer(owner_token))
    assert [row["ticket_id"] for row in links.json()] == [ticket_id]

    on_ticket = client.get(f"/v1/tickets/{ticket_id}/memory", headers=_bearer(owner_token))
    assert on_ticket.status_code == 200
    pairs = on_ticket.json()
    assert len(pairs) == 1
    assert pairs[0]["link"]["reason"] == "explains the constraint"
    assert pairs[0]["entry"]["title"] == "Context"
    assert pairs[0]["entry"]["provenance"]["actor_id"]  # provenance travels


def test_link_integrity_over_http(client: TestClient, owner_token: str) -> None:
    ticket_id = _create_ticket(client, owner_token)
    created = _create_memory(client, owner_token)

    missing_ticket = client.post(
        f"/v1/memory/{created['id']}/link",
        json={"ticket_id": "tkt_missing", "reason": "r"},
        headers=_bearer(owner_token),
    )
    assert missing_ticket.status_code == 404

    missing_memory = client.post(
        "/v1/memory/mem_missing/link",
        json={"ticket_id": ticket_id, "reason": "r"},
        headers=_bearer(owner_token),
    )
    assert missing_memory.status_code == 404

    first = client.post(
        f"/v1/memory/{created['id']}/link",
        json={"ticket_id": ticket_id, "reason": "first"},
        headers=_bearer(owner_token),
    )
    assert first.status_code == 201
    duplicate = client.post(
        f"/v1/memory/{created['id']}/link",
        json={"ticket_id": ticket_id, "reason": "again"},
        headers=_bearer(owner_token),
    )
    assert duplicate.status_code == 422
    assert "already linked" in duplicate.json()["detail"]


def test_delete_cascades_links(client: TestClient, owner_token: str, engine: Engine) -> None:
    ticket_id = _create_ticket(client, owner_token)
    created = _create_memory(client, owner_token)
    client.post(
        f"/v1/memory/{created['id']}/link",
        json={"ticket_id": ticket_id, "reason": "r"},
        headers=_bearer(owner_token),
    )

    assert (
        client.delete(f"/v1/memory/{created['id']}", headers=_bearer(owner_token)).status_code
        == 204
    )
    with Session(engine) as session:
        assert session.exec(select(MemoryLink)).all() == []
    on_ticket = client.get(f"/v1/tickets/{ticket_id}/memory", headers=_bearer(owner_token))
    assert on_ticket.json() == []


# ----------------------------------------------------------------- promotion


def test_agent_can_propose_but_cannot_approve(
    client: TestClient, owner_token: str, agent_token: str
) -> None:
    """E13-T4 propose-first: memory.write proposes; memory.approve is human-only."""
    team = _create_memory(client, owner_token, title="shared")

    # The agent CAN promote (it holds memory.write).
    promoted = client.post(f"/v1/memory/{team['id']}/promote", headers=_bearer(agent_token))
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["review_status"] == "proposed"

    # The agent CANNOT approve — its token never carries memory.approve (403).
    denied = client.post(f"/v1/memory/{team['id']}/approve", headers=_bearer(agent_token))
    assert denied.status_code == 403


def test_viewer_cannot_approve_or_reject(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    team = _create_memory(client, owner_token, title="shared")
    client.post(f"/v1/memory/{team['id']}/promote", headers=_bearer(owner_token))

    assert (
        client.post(f"/v1/memory/{team['id']}/approve", headers=_bearer(viewer_token)).status_code
        == 403
    )
    assert (
        client.post(f"/v1/memory/{team['id']}/reject", headers=_bearer(viewer_token)).status_code
        == 403
    )
    # And a Viewer cannot even propose (no memory.write).
    assert (
        client.post(f"/v1/memory/{team['id']}/promote", headers=_bearer(viewer_token)).status_code
        == 403
    )


def test_promotion_routes_require_a_token(client: TestClient) -> None:
    for verb in ("promote", "approve", "reject"):
        assert client.post(f"/v1/memory/mem_x/{verb}").status_code == 401


def test_local_to_proposed_to_approved_happy_path(
    client: TestClient, owner_token: str, agent_token: str
) -> None:
    """Create local → promote (new team proposed row) → owner approves → shared."""
    local = _create_memory(client, owner_token, title="rationale", visibility="local")
    assert local["domain_visibility"] == "private_local"

    # The agent proposes; a NEW team row comes back at proposed.
    promoted = client.post(f"/v1/memory/{local['id']}/promote", headers=_bearer(agent_token))
    assert promoted.status_code == 200, promoted.text
    proposed = promoted.json()
    assert proposed["id"] != local["id"]
    assert proposed["visibility"] == "team"
    assert proposed["review_status"] == "proposed"
    assert proposed["domain_visibility"] == "proposal_context"

    # The original local row is untouched.
    refreshed = client.get(f"/v1/memory/{local['id']}", headers=_bearer(owner_token)).json()
    assert refreshed["visibility"] == "local"
    assert refreshed["review_status"] == "draft"

    # The owner approves the proposed row → shared.
    approved = client.post(f"/v1/memory/{proposed['id']}/approve", headers=_bearer(owner_token))
    assert approved.status_code == 200, approved.text
    assert approved.json()["review_status"] == "approved"
    assert approved.json()["domain_visibility"] == "shared_workspace"


def test_approved_project_scoped_entry_is_shared_project(
    client: TestClient, owner_token: str, agent_token: str
) -> None:
    """E13-T5 sixth state: an approved entry whose space is not ``workspace``
    shares at project scope — ``shared_project`` (vs ``shared_workspace`` above)."""
    local = _create_memory(client, owner_token, title="why B", space="project", visibility="local")
    proposed = client.post(f"/v1/memory/{local['id']}/promote", headers=_bearer(agent_token)).json()
    assert proposed["space"] == "project"
    approved = client.post(f"/v1/memory/{proposed['id']}/approve", headers=_bearer(owner_token))
    assert approved.status_code == 200, approved.text
    assert approved.json()["domain_visibility"] == "shared_project"


def test_approve_an_already_decided_entry_409(client: TestClient, owner_token: str) -> None:
    team = _create_memory(client, owner_token, title="shared")
    client.post(f"/v1/memory/{team['id']}/promote", headers=_bearer(owner_token))
    assert (
        client.post(f"/v1/memory/{team['id']}/reject", headers=_bearer(owner_token)).status_code
        == 200
    )
    # Approving the now-rejected row loses the CAS → 409.
    again = client.post(f"/v1/memory/{team['id']}/approve", headers=_bearer(owner_token))
    assert again.status_code == 409


def test_promote_already_proposed_team_row_422(client: TestClient, owner_token: str) -> None:
    team = _create_memory(client, owner_token, title="shared")
    client.post(f"/v1/memory/{team['id']}/promote", headers=_bearer(owner_token))
    again = client.post(f"/v1/memory/{team['id']}/promote", headers=_bearer(owner_token))
    assert again.status_code == 422
    assert "cannot be promoted" in again.json()["detail"]


def test_promote_missing_entry_404(client: TestClient, owner_token: str) -> None:
    assert (
        client.post("/v1/memory/mem_missing/promote", headers=_bearer(owner_token)).status_code
        == 404
    )


def test_local_promote_through_the_api_emits_no_events_for_the_local_row(
    client: TestClient, owner_token: str, engine: Engine
) -> None:
    """The runtime wires the real sink; promoting a local entry leaves it silent."""
    local = _create_memory(client, owner_token, title="private", visibility="local")
    promoted = client.post(f"/v1/memory/{local['id']}/promote", headers=_bearer(owner_token)).json()

    with Session(engine) as session:
        rows = session.exec(select(EventLog)).all()
    memory_rows = [r for r in rows if r.collection == "memory_entries"]
    # No event references the local row id; the new team row did emit.
    assert local["id"] not in {r.entity_id for r in memory_rows}
    assert promoted["id"] in {r.entity_id for r in memory_rows}


# -------------------------------------------------------------------- privacy


def test_local_writes_through_the_api_emit_no_events(
    client: TestClient, owner_token: str, engine: Engine
) -> None:
    """The runtime wires the real sink; a local entry still never reaches it."""
    ticket_id = _create_ticket(client, owner_token)
    created = _create_memory(client, owner_token, title="private", visibility="local")
    client.patch(
        f"/v1/memory/{created['id']}", json={"body": "secret"}, headers=_bearer(owner_token)
    )
    client.post(
        f"/v1/memory/{created['id']}/link",
        json={"ticket_id": ticket_id, "reason": "private"},
        headers=_bearer(owner_token),
    )
    client.delete(f"/v1/memory/{created['id']}", headers=_bearer(owner_token))

    with Session(engine) as session:
        rows = session.exec(select(EventLog)).all()
    assert [r for r in rows if r.collection in {"memory_entries", "memory_links"}] == []


def test_team_writes_through_the_api_emit_events(
    client: TestClient, owner_token: str, engine: Engine
) -> None:
    created = _create_memory(client, owner_token, title="shared")
    with Session(engine) as session:
        rows = session.exec(select(EventLog)).all()
    memory_rows = [r for r in rows if r.collection == "memory_entries"]
    assert [r.entity_id for r in memory_rows] == [created["id"]]
