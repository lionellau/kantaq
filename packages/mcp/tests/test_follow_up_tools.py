"""The v0.3 follow-up tools through the gateway (E15-T1 / MOD-29).

``follow_up_create``/``update``/``complete`` are propose-first: an agent's call
stores a pending ``agent_proposal`` (a ``{kind: follow_up.*}`` diff) and writes
NO follow_up row — a human approval through the **one apply path**
(``proposals.approve_proposal``) creates / edits / completes it. The Inbox
proposal is the only thing that syncs until then. ``follow_up_search`` reads,
fencing the human-authored title/body untrusted.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq_core import proposals
from kantaq_core.identity import MintedToken
from kantaq_core.tracker.service import TrackerService
from kantaq_db.models import AgentProposal, FollowUp
from kantaq_db.models import Ticket as TicketModel
from kantaq_mcp.gateway import Gateway
from kantaq_mcp.tools import ToolError


def _call(gateway: Gateway, minted: MintedToken, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    actor = gateway.authenticate(minted.plaintext)
    assert actor is not None
    session = gateway.session_for(actor, session_id=f"s-{minted.member_id}")
    return gateway.handle_call(actor=actor, session=session, tool_name=tool, args=args)


def _approve(engine: Engine, proposal_id: str, approver: MintedToken) -> None:
    with Session(engine) as session:
        proposals.approve_proposal(session, proposal_id, actor_id=approver.member_id, source="app")


def test_follow_up_create_stores_a_proposal_not_a_follow_up(
    gateway: Gateway, agent: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    result = _call(
        gateway,
        agent,
        "follow_up_create",
        {"ticket_id": ticket.id, "title": "check the deploy", "due_at": "2026-09-01T00:00:00"},
    )
    assert result["applied"] is False
    proposal = result["proposal"]
    assert proposal["status"] == "pending"
    assert proposal["ticket_id"] == ticket.id
    assert proposal["diff"]["kind"] == "follow_up.create"
    assert proposal["diff"]["follow_up"]["title"] == "check the deploy"
    # provenance records the agent proposer, surviving a later approval.
    assert proposal["diff"]["follow_up"]["provenance"]["origin"] == "agent"
    # propose-first: NO follow_up row yet — only the proposal row + its event.
    with Session(engine) as session:
        assert session.exec(select(FollowUp)).all() == []
        rows = session.exec(select(AgentProposal)).all()
        assert len(rows) == 1 and rows[0].diff["kind"] == "follow_up.create"


def test_follow_up_create_unknown_ticket_is_a_domain_error_not_a_denial(
    gateway: Gateway, agent: MintedToken, ticket: TicketModel, audit_rows: Any
) -> None:
    with pytest.raises(ToolError) as err:
        _call(
            gateway,
            agent,
            "follow_up_create",
            {"ticket_id": "01TICKETDOESNOTEXIST00000", "title": "t"},
        )
    assert err.value.code == "not_found"
    assert audit_rows("tool.deny") == []


def test_follow_up_create_requires_a_title(
    gateway: Gateway, agent: MintedToken, ticket: TicketModel
) -> None:
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "follow_up_create", {"ticket_id": ticket.id, "title": "  "})
    assert err.value.code == "validation"


def test_follow_up_create_then_approve_creates_the_row(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    result = _call(
        gateway,
        agent,
        "follow_up_create",
        {"ticket_id": ticket.id, "title": "revisit", "body": "later"},
    )
    _approve(engine, result["proposal"]["id"], owner)
    with Session(engine) as session:
        rows = session.exec(select(FollowUp)).all()
        assert len(rows) == 1
        assert rows[0].title == "revisit"
        assert rows[0].body == "later"
        assert rows[0].status == "open"
        assert rows[0].ticket_id == ticket.id
        # provenance carried over from the agent's proposal, not the approver.
        assert rows[0].provenance["origin"] == "agent"


def test_follow_up_complete_round_trip_through_proposals(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    created = _call(gateway, agent, "follow_up_create", {"ticket_id": ticket.id, "title": "rt"})
    _approve(engine, created["proposal"]["id"], owner)
    with Session(engine) as session:
        follow_up_id = session.exec(select(FollowUp)).one().id

    done = _call(
        gateway, agent, "follow_up_complete", {"follow_up_id": follow_up_id, "status": "done"}
    )
    assert done["proposal"]["diff"]["kind"] == "follow_up.complete"
    # propose-first: still open until the human approves.
    with Session(engine) as session:
        assert session.get(FollowUp, follow_up_id).status == "open"
    _approve(engine, done["proposal"]["id"], owner)
    with Session(engine) as session:
        assert session.get(FollowUp, follow_up_id).status == "done"


def test_follow_up_update_round_trip_through_proposals(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    created = _call(gateway, agent, "follow_up_create", {"ticket_id": ticket.id, "title": "old"})
    _approve(engine, created["proposal"]["id"], owner)
    with Session(engine) as session:
        follow_up_id = session.exec(select(FollowUp)).one().id

    edit = _call(
        gateway,
        agent,
        "follow_up_update",
        {"follow_up_id": follow_up_id, "changes": {"title": "new title"}},
    )
    assert edit["proposal"]["diff"]["kind"] == "follow_up.update"
    _approve(engine, edit["proposal"]["id"], owner)
    with Session(engine) as session:
        assert session.get(FollowUp, follow_up_id).title == "new title"


def test_follow_up_search_fences_prose_and_returns_rows(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    with Session(engine) as session:
        service = TrackerService(session, actor_id=owner.member_id, source="app")
        service.create_follow_up(ticket_id=ticket.id, title="visible work", body="b")
    result = _call(gateway, agent, "follow_up_search", {"ticket_id": ticket.id})
    assert result["count"] == 1
    row = result["follow_ups"][0]
    assert row["title"] == '<untrusted source="follow_up.title">visible work</untrusted>'
    assert row["status"] == "open"


def test_follow_up_complete_unknown_id_is_a_domain_error(
    gateway: Gateway, agent: MintedToken, ticket: TicketModel
) -> None:
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "follow_up_complete", {"follow_up_id": "flw_missing0000000000000"})
    assert err.value.code == "not_found"
