"""Proposals API over HTTP (E20, MOD-12's server half).

Pins the Inbox contract: the queue lists what agents proposed, Approve applies
the diff through the one tracker write path **atomically** with the status
flip, Reject declines without touching the ticket, and audit shows the
proposer and the approver as distinct actors (sprint-2 dogfood-gate #4).
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
from kantaq_db.models import AgentProposal, AuditEvent, EventLog, Ticket
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


@pytest.fixture
def viewer_token(engine: Engine, owner_token: str) -> str:
    with Session(engine) as session:
        return (
            IdentityService(session).invite(email="viewer@example.com", role=Role.viewer).plaintext
        )


@pytest.fixture
def agent(engine: Engine, owner_token: str) -> tuple[str, str]:
    """An Agent member with the propose scope: (member_id, token)."""
    with Session(engine) as session:
        minted = IdentityService(session).invite(
            email="agent@example.com",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
    return minted.member_id, minted.plaintext


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


def _propose(
    engine: Engine, agent_id: str, ticket_id: str, changes: dict[str, Any], note: str = ""
) -> str:
    """Create a proposal through the real propose path (the MOD-09 tool)."""
    with Session(engine) as session:
        result = agent_action_propose(
            session,
            actor_id=agent_id,
            args={"ticket_id": ticket_id, "changes": changes, "note": note},
            now=lambda: datetime.now(UTC).replace(tzinfo=None),
        )
    proposal_id: str = result["proposal"]["id"]
    return proposal_id


# -------------------------------------------------------------------- authz


def test_proposals_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/proposals").status_code == 401
    assert client.post("/v1/proposals/p1/approve").status_code == 401
    assert client.post("/v1/proposals/p1/reject").status_code == 401


def test_viewer_reads_the_queue_but_cannot_decide(
    client: TestClient, engine: Engine, owner_token: str, viewer_token: str, agent: tuple[str, str]
) -> None:
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent[0], ticket["id"], {"status": "doing"})

    assert client.get("/v1/proposals", headers=_bearer(viewer_token)).status_code == 200
    deny = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(viewer_token))
    assert deny.status_code == 403
    deny = client.post(f"/v1/proposals/{proposal_id}/reject", headers=_bearer(viewer_token))
    assert deny.status_code == 403


def test_an_agent_cannot_approve_its_own_proposal(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    """proposals.write does not imply tickets.write — propose-first stays closed."""
    agent_id, agent_token = agent
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent_id, ticket["id"], {"status": "doing"})

    deny = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(agent_token))
    assert deny.status_code == 403
    with Session(engine) as session:
        proposal = session.get(AgentProposal, proposal_id)
        assert proposal is not None and proposal.status == "pending"


# --------------------------------------------------------------------- list


def test_list_filters_by_status_and_ticket(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    ticket_a = _create_ticket(client, owner_token, title="Ticket A")
    ticket_b = _create_ticket(client, owner_token, title="Ticket B")
    first = _propose(engine, agent[0], ticket_a["id"], {"status": "doing"}, note="move it")
    _propose(engine, agent[0], ticket_b["id"], {"priority": "high"})

    everything = client.get("/v1/proposals", headers=_bearer(owner_token)).json()
    assert {p["id"] for p in everything} >= {first}
    assert all(p["status"] == "pending" for p in everything)

    by_ticket = client.get(
        "/v1/proposals", params={"ticket": ticket_a["id"]}, headers=_bearer(owner_token)
    ).json()
    assert [p["id"] for p in by_ticket] == [first]
    assert by_ticket[0]["ticket_title"] == "Ticket A"
    assert by_ticket[0]["diff"] == {"changes": {"status": "doing"}, "note": "move it"}

    assert (
        client.get(
            "/v1/proposals", params={"status": "bogus"}, headers=_bearer(owner_token)
        ).status_code
        == 422
    )


# ------------------------------------------------------------------- approve


def test_approve_applies_the_change_and_audits_both_actors(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    agent_id, _ = agent
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent_id, ticket["id"], {"status": "doing"})

    response = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal"]["status"] == "approved"
    assert body["ticket"]["status"] == "doing"

    with Session(engine) as session:
        row = session.get(Ticket, ticket["id"])
        assert row is not None and row.status == "doing"

        audits = list(session.exec(select(AuditEvent)).all())
        create = next(a for a in audits if a.action == "proposal.create")
        approve = next(a for a in audits if a.action == "proposal.approve")
        update = next(a for a in audits if a.action == "ticket.update")
        # Dogfood-gate #4: proposer and approver are distinct actors.
        assert create.actor_id == agent_id
        assert approve.actor_id != agent_id
        assert update.actor_id == approve.actor_id

        # Both the decision and the applied patch sync (events emitted).
        events = list(session.exec(select(EventLog)).all())
        assert any(e.collection == "agent_proposals" and e.entity_id == proposal_id for e in events)
        assert sum(1 for e in events if e.collection == "tickets") >= 2  # create + update


def test_approve_is_atomic_when_the_diff_fails_validation(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    """An invalid value 422s at apply time and leaves the proposal pending."""
    ticket = _create_ticket(client, owner_token)
    # Forge an out-of-vocabulary status directly on the row: the tool itself
    # would refuse it, but a synced replica must not trust remote diffs either.
    proposal_id = _propose(engine, agent[0], ticket["id"], {"status": "doing"})
    with Session(engine) as session:
        proposal = session.get(AgentProposal, proposal_id)
        assert proposal is not None
        proposal.diff = {"changes": {"status": "not-a-status"}, "note": ""}
        session.add(proposal)
        session.commit()

    response = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert response.status_code == 422

    with Session(engine) as session:
        proposal = session.get(AgentProposal, proposal_id)
        assert proposal is not None and proposal.status == "pending"
        row = session.get(Ticket, ticket["id"])
        assert row is not None and row.status == "todo"
        assert not any(
            a.action in ("proposal.approve", "ticket.update")
            for a in session.exec(select(AuditEvent)).all()
        )


def test_approve_refuses_a_decided_proposal(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent[0], ticket["id"], {"status": "doing"})

    assert (
        client.post(f"/v1/proposals/{proposal_id}/reject", headers=_bearer(owner_token)).status_code
        == 200
    )
    again = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert again.status_code == 409
    missing = client.post("/v1/proposals/missing/approve", headers=_bearer(owner_token))
    assert missing.status_code == 404


def test_approve_refuses_an_empty_diff(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent[0], ticket["id"], {"status": "doing"})
    with Session(engine) as session:
        proposal = session.get(AgentProposal, proposal_id)
        assert proposal is not None
        proposal.diff = {"changes": {}, "note": ""}
        session.add(proposal)
        session.commit()

    response = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert response.status_code == 422


# -------------------------------------------------------------------- reject


def test_reject_declines_without_touching_the_ticket(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    agent_id, _ = agent
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent_id, ticket["id"], {"status": "doing"})

    response = client.post(f"/v1/proposals/{proposal_id}/reject", headers=_bearer(owner_token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    with Session(engine) as session:
        row = session.get(Ticket, ticket["id"])
        assert row is not None and row.status == "todo"
        reject = next(
            a for a in session.exec(select(AuditEvent)).all() if a.action == "proposal.reject"
        )
        assert reject.actor_id != agent_id
        events = list(session.exec(select(EventLog)).all())
        assert any(e.collection == "agent_proposals" and e.entity_id == proposal_id for e in events)


# ------------------------------------------- SEC second-review regressions


def test_flip_loses_to_a_concurrent_decision(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    """The status flip is a compare-and-swap: a decision that raced and lost
    409s instead of applying on top of the winner (double-apply guard)."""
    from fastapi import HTTPException

    from kantaq_runtime.proposals_api import _flip_status, _now

    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent[0], ticket["id"], {"status": "doing"})

    with Session(engine) as stale:
        proposal = stale.get(AgentProposal, proposal_id)
        assert proposal is not None
        stale.rollback()  # release the read so the winner can commit

        winner = client.post(f"/v1/proposals/{proposal_id}/reject", headers=_bearer(owner_token))
        assert winner.status_code == 200

        with pytest.raises(HTTPException) as denied:
            _flip_status(stale, proposal, actor_id="someone", status="approved", ts=_now())
        assert denied.value.status_code == 409

    with Session(engine) as session:
        row = session.get(AgentProposal, proposal_id)
        assert row is not None and row.status == "rejected"
        decisions = [
            a
            for a in session.exec(select(AuditEvent)).all()
            if a.action in ("proposal.approve", "proposal.reject")
        ]
        assert [a.action for a in decisions] == ["proposal.reject"]


def test_approve_refuses_unknown_diff_fields(
    client: TestClient, engine: Engine, owner_token: str, agent: tuple[str, str]
) -> None:
    """TicketPatch is extra="forbid": a forged diff key 422s instead of being
    silently dropped (a synced replica must not trust remote diffs)."""
    ticket = _create_ticket(client, owner_token)
    proposal_id = _propose(engine, agent[0], ticket["id"], {"status": "doing"})
    with Session(engine) as session:
        proposal = session.get(AgentProposal, proposal_id)
        assert proposal is not None
        proposal.diff = {"changes": {"status": "doing", "created_by": "evil"}, "note": ""}
        session.add(proposal)
        session.commit()

    response = client.post(f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner_token))
    assert response.status_code == 422
    with Session(engine) as session:
        row = session.get(AgentProposal, proposal_id)
        assert row is not None and row.status == "pending"
