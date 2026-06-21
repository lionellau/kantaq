"""The v0.3 dependency-graph tools through the gateway (E15-T2 / MOD-29).

dependency_graph_get + dependency_path_find are read tools (the agent's
tickets.read grant authorizes them) over the blocks family of
ticket_relationships. path_find returns the blocking chain, or a structured
cycle_detected result naming the offending tickets when legacy data holds a cycle.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import MintedToken
from kantaq_core.tracker import TrackerService
from kantaq_db.models import Ticket as TicketModel
from kantaq_db.models import TicketRelationship
from kantaq_mcp.gateway import Gateway
from kantaq_mcp.tools import ToolError


def _call(gateway: Gateway, minted: MintedToken, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    actor = gateway.authenticate(minted.plaintext)
    assert actor is not None
    session = gateway.session_for(actor, session_id=f"s-{minted.member_id}")
    return gateway.handle_call(actor=actor, session=session, tool_name=tool, args=args)


def _chain(engine: Engine, owner_id: str, project_id: str) -> tuple[str, str, str]:
    """a blocks b blocks c, created through the real relation path (owner)."""
    with Session(engine) as session:
        service = TrackerService(session, actor_id=owner_id, source="app")
        a = service.create_ticket(project_id=project_id, title="a")
        b = service.create_ticket(project_id=project_id, title="b")
        c = service.create_ticket(project_id=project_id, title="c")
        service.add_relation(a.id, b.id, "blocking")
        service.add_relation(b.id, c.id, "blocking")
        return a.id, b.id, c.id


def test_path_find_returns_the_blocking_path(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    a, b, c = _chain(engine, owner.member_id, ticket.project_id)
    result = _call(gateway, agent, "dependency_path_find", {"from_ticket_id": a, "to_ticket_id": c})
    assert result["found"] is True
    assert result["path"] == [a, b, c]
    assert result["cycle_detected"] is False


def test_path_find_reports_a_legacy_cycle(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    a, b, c = _chain(engine, owner.member_id, ticket.project_id)
    # A legacy cycle the create-guard would reject — insert it directly.
    with Session(engine) as session:
        session.add(TicketRelationship(from_id=c, to_id=a, type="blocking"))
        session.commit()
    result = _call(gateway, agent, "dependency_path_find", {"from_ticket_id": a, "to_ticket_id": c})
    assert result["found"] is False
    assert result["cycle_detected"] is True
    assert set(result["cycle"]) == {a, b, c}


def test_graph_get_returns_nodes_and_edges(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    a, b, c = _chain(engine, owner.member_id, ticket.project_id)
    result = _call(gateway, agent, "dependency_graph_get", {"root_ticket_id": a})
    assert set(result["nodes"]) == {a, b, c}
    assert {"blocks": a, "blocked": b} in result["edges"]
    assert result["node_count"] == 3 and result["edge_count"] == 2


def test_graph_get_depth_bounds_the_walk(
    gateway: Gateway, agent: MintedToken, owner: MintedToken, ticket: TicketModel, engine: Engine
) -> None:
    a, b, _ = _chain(engine, owner.member_id, ticket.project_id)
    result = _call(gateway, agent, "dependency_graph_get", {"root_ticket_id": a, "depth": 1})
    assert set(result["nodes"]) == {a, b}  # only one hop from a


def test_path_find_unknown_ticket_is_a_domain_error(
    gateway: Gateway, agent: MintedToken, ticket: TicketModel
) -> None:
    with pytest.raises(ToolError) as err:
        _call(
            gateway,
            agent,
            "dependency_path_find",
            {"from_ticket_id": "01TICKETDOESNOTEXIST00000", "to_ticket_id": ticket.id},
        )
    assert err.value.code == "not_found"


def test_path_find_requires_both_ids(
    gateway: Gateway, agent: MintedToken, ticket: TicketModel
) -> None:
    with pytest.raises(ToolError) as err:
        _call(gateway, agent, "dependency_path_find", {"from_ticket_id": ticket.id})
    assert err.value.code == "validation"
