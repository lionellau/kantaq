"""The two v0.0.5 tools through the gateway (E10-T1, E10-T2).

ticket_get wraps every human-authored string untrusted; agent_action_propose
stores a pending proposal + its sync event + its detailed audit row and never
touches the ticket. The PROPOSABLE_FIELDS contract test pins this package's
field allowlist against the tracker's (MOD-03 owns that set; drift must fail
loudly, not silently widen what agents can propose).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq_core.identity import MintedToken
from kantaq_core.tracker.service import _TICKET_PATCHABLE, TrackerService
from kantaq_db.models import AgentProposal, AuditEvent, EventLog, Ticket
from kantaq_mcp.gateway import Gateway
from kantaq_mcp.tools import PROPOSABLE_FIELDS, ToolError

AuditProbe = Callable[..., list[AuditEvent]]


def _call(gateway: Gateway, minted: MintedToken, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    actor = gateway.authenticate(minted.plaintext)
    assert actor is not None
    session = gateway.session_for(actor, session_id=f"s-{minted.member_id}")
    return gateway.handle_call(actor=actor, session=session, tool_name=tool, args=args)


def test_proposable_fields_match_the_tracker_patch_allowlist() -> None:
    assert PROPOSABLE_FIELDS == _TICKET_PATCHABLE


def test_ticket_get_wraps_every_human_authored_string(
    gateway: Gateway, agent: MintedToken, ticket: Ticket
) -> None:
    result = _call(gateway, agent, "ticket_get", {"ticket_id": ticket.id})
    body = result["ticket"]
    assert body["id"] == ticket.id
    assert body["title"] == (
        '<untrusted source="ticket.title">Wire the loopback gateway</untrusted>'
    )
    assert body["description"].startswith('<untrusted source="ticket.description">')
    assert body["acceptance_criteria"].startswith('<untrusted source="ticket.acceptance_criteria">')
    assert body["labels"] == [
        '<untrusted source="ticket.label">mcp</untrusted>',
        '<untrusted source="ticket.label">security</untrusted>',
    ]
    # Domain-validated enums and ids stay raw — they are not human prose.
    assert body["status"] == "todo"
    assert body["priority"] == "medium"
    assert body["project_id"] == ticket.project_id


def test_ticket_get_unknown_ticket_is_a_domain_error_not_a_denial(
    gateway: Gateway, agent: MintedToken, ticket: Ticket, audit_rows: AuditProbe
) -> None:
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "ticket_get", {"ticket_id": "01TICKETDOESNOTEXIST00000"})
    assert err.value.code == "not_found"
    assert audit_rows("tool.deny") == []


def test_ticket_get_requires_a_ticket_id_even_without_schema_validation(
    gateway: Gateway, agent: MintedToken, ticket: Ticket
) -> None:
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "ticket_get", {})
    assert err.value.code == "validation"


def test_milestone_get_fences_prose_and_returns_grouped_tickets(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: Ticket, engine: Engine
) -> None:
    """milestone_get goes through the eight checks (the agent's tickets.read
    grant authorizes it) and fences the human-authored name/description."""
    ticket_id = ticket.id
    with Session(engine) as session:
        service = TrackerService(session, actor_id=owner.member_id, source="app")
        milestone = service.create_milestone(
            project_id=ticket.project_id, name="v1.0 launch", description="ship it"
        )
        milestone_id = milestone.id
        service.add_ticket_to_milestone(ticket_id, milestone_id)

    result = _call(gateway, agent, "milestone_get", {"milestone_id": milestone_id})
    body = result["milestone"]
    assert body["name"] == '<untrusted source="milestone.name">v1.0 launch</untrusted>'
    assert body["description"] == '<untrusted source="milestone.description">ship it</untrusted>'
    # Domain enums + ids stay raw; the grouped ticket is returned for scope.
    assert body["status"] == "active"
    assert body["ticket_ids"] == [ticket.id]
    assert body["ticket_count"] == 1


def test_milestone_get_unknown_is_a_domain_error_not_a_denial(
    gateway: Gateway, agent: MintedToken, ticket: Ticket, audit_rows: AuditProbe
) -> None:
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "milestone_get", {"milestone_id": "01MILESTONEMISSING0000000"})
    assert err.value.code == "not_found"
    assert audit_rows("tool.deny") == []


def test_propose_stores_a_pending_proposal_and_never_touches_the_ticket(
    gateway: Gateway,
    engine: Engine,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
) -> None:
    before_updated_at = ticket.updated_at
    result = _call(
        gateway,
        agent,
        "agent_action_propose",
        {
            "ticket_id": ticket.id,
            "changes": {"status": "done"},
            "note": "All acceptance criteria pass.",
        },
    )

    assert result["applied"] is False
    proposal_out = result["proposal"]
    assert proposal_out["status"] == "pending"
    assert proposal_out["diff"] == {
        "changes": {"status": "done"},
        "note": "All acceptance criteria pass.",
    }
    assert proposal_out["proposer_id"] == agent.member_id

    with Session(engine) as session:
        stored = session.get(AgentProposal, proposal_out["id"])
        assert stored is not None and stored.status == "pending"
        fresh_ticket = session.get(Ticket, ticket.id)
        assert fresh_ticket is not None
        assert fresh_ticket.status == "todo", "propose must not change the ticket"
        assert fresh_ticket.updated_at == before_updated_at
        # The proposal syncs: one event-log row for the agent_proposals collection.
        events = session.exec(
            select(EventLog).where(EventLog.collection == "agent_proposals")
        ).all()
        assert [e.entity_id for e in events] == [proposal_out["id"]]
        assert events[0].actor_id == agent.member_id

    # Agent writes are audited in detail, attributed to the proposing agent.
    detailed = audit_rows("proposal.create")
    assert len(detailed) == 1
    assert detailed[0].actor_id == agent.member_id
    assert detailed[0].source == "mcp"
    assert detailed[0].object_ref == f"agent_proposals/{proposal_out['id']}"


@pytest.mark.parametrize(
    ("args_patch", "expected_code"),
    [
        ({"changes": {"created_by": "evil"}}, "validation"),  # not proposable
        ({"changes": {}}, "validation"),
        ({"changes": "done"}, "validation"),
        ({"changes": {"status": "shipped"}}, "validation"),  # unknown enum value
        ({"changes": {"priority": "asap"}}, "validation"),
        ({"changes": {"status": "done"}, "note": "x" * 2001}, "validation"),
        ({"changes": {"status": "done"}, "ticket_id": "01NOPE00000000000000000000"}, "not_found"),
    ],
)
def test_propose_validation_fails_closed(
    gateway: Gateway,
    engine: Engine,
    agent: MintedToken,
    ticket: Ticket,
    args_patch: dict[str, Any],
    expected_code: str,
) -> None:
    args: dict[str, Any] = {"ticket_id": ticket.id, "changes": {"status": "done"}}
    args.update(args_patch)
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "agent_action_propose", args)
    assert err.value.code == expected_code
    with Session(engine) as session:
        assert session.exec(select(AgentProposal)).all() == []
        assert (
            session.exec(select(EventLog).where(EventLog.collection == "agent_proposals")).all()
            == []
        )
