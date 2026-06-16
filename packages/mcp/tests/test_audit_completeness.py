"""Audit completeness for a scripted agent session (MOD-08 required test).

Every MCP call lands in the audit log under the v0.0.5 policy: reads roll up
to one ``agent.read`` summary per agent (the gateway owns the flush cadence),
writes get a detailed ``proposal.create`` row, denials get ``tool.deny`` —
all attributed to the acting member with ``source="mcp"``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from kantaq_core.identity import MintedToken, VerifiedActor
from kantaq_db.models import AuditEvent, Ticket
from kantaq_mcp.gateway import Gateway, GatewayDenied
from kantaq_mcp.session import GatewaySession
from kantaq_test_harness.clock import FakeClock

AuditProbe = Callable[..., list[AuditEvent]]


def _session_for(gateway: Gateway, minted: MintedToken) -> tuple[VerifiedActor, GatewaySession]:
    actor = gateway.authenticate(minted.plaintext)
    assert actor is not None
    return actor, gateway.session_for(actor, session_id=f"s-{minted.member_id}")


def test_scripted_session_leaves_a_complete_trail(
    gateway: Gateway,
    engine: Engine,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
) -> None:
    actor, session = _session_for(gateway, agent)

    def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return gateway.handle_call(actor=actor, session=session, tool_name=tool, args=args)

    call("ticket_get", {"ticket_id": ticket.id})
    call("ticket_get", {"ticket_id": ticket.id})
    call(
        "agent_action_propose",
        {"ticket_id": ticket.id, "changes": {"status": "doing"}, "note": "starting"},
    )
    with pytest.raises(GatewayDenied):
        call("permission_grant", {})

    flushed = gateway.flush_reads()
    assert flushed == 1, "one summary row per agent"

    reads = audit_rows("agent.read")
    assert len(reads) == 1
    assert reads[0].actor_id == agent.member_id
    assert reads[0].source == "mcp"
    assert reads[0].after is not None
    assert reads[0].after["reads"] == 2
    assert reads[0].after["objects"] == {f"tickets/{ticket.id}": 2}
    # MOD-08: the gateway tallies the JSON payload size of each read, so the
    # summary carries real bytes (the feed for metrics' est_tokens), not 0.
    assert reads[0].after["bytes"] > 0

    proposals = audit_rows("proposal.create")
    assert [row.actor_id for row in proposals] == [agent.member_id]

    denials = audit_rows("tool.deny")
    assert len(denials) == 1
    assert denials[0].after is not None
    assert denials[0].after["reason"] == "tool_allowlist"

    # Nothing in the trail is attributed to anyone but the acting agent.
    mcp_rows = [row for row in audit_rows() if row.source == "mcp"]
    assert {row.actor_id for row in mcp_rows} == {agent.member_id}


def test_read_flush_cadence_is_time_driven(
    gateway: Gateway,
    clock: FakeClock,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
) -> None:
    actor, session = _session_for(gateway, agent)

    def read() -> None:
        gateway.handle_call(
            actor=actor, session=session, tool_name="ticket_get", args={"ticket_id": ticket.id}
        )

    read()
    assert audit_rows("agent.read") == [], "within the interval reads stay aggregated"

    clock.advance(61)
    read()  # crossing the interval flushes on the next read
    rows = audit_rows("agent.read")
    assert len(rows) == 1
    assert rows[0].after is not None and rows[0].after["reads"] == 2

    # An idle gateway has nothing to flush; the explicit flush is a no-op.
    clock.advance(61)
    assert gateway.flush_reads() == 0
    assert len(audit_rows("agent.read")) == 1
