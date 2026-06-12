"""Gateway telemetry capture: session starts, nothing else (E28, MOD-25)."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from kantaq_core.identity import MintedToken
from kantaq_mcp.gateway import Gateway
from kantaq_test_harness.telemetry import TelemetryCapture


def test_new_session_records_one_event_with_member_id_only(
    gateway: Gateway, engine: Engine, agent: MintedToken
) -> None:
    capture = TelemetryCapture(engine)
    capture.enable()
    actor = gateway.authenticate(agent.plaintext)
    assert actor is not None
    gateway.session_for(actor, session_id="s-1")
    gateway.session_for(actor, session_id="s-1")  # same transport session: no new event

    rows = capture.events()
    assert [row.name for row in rows] == ["mcp_session_started"]
    assert rows[0].props == {"member_id": agent.member_id}
    # Never the token or its scopes — the member ULID is the only prop.
    assert agent.plaintext not in str(rows[0].props)


def test_a_second_transport_session_records_again(
    gateway: Gateway, engine: Engine, agent: MintedToken
) -> None:
    capture = TelemetryCapture(engine)
    capture.enable()
    actor = gateway.authenticate(agent.plaintext)
    assert actor is not None
    gateway.session_for(actor, session_id="s-1")
    gateway.session_for(actor, session_id="s-2")
    assert len(capture.events()) == 2


def test_opted_out_gateway_records_nothing(
    gateway: Gateway, engine: Engine, agent: MintedToken
) -> None:
    actor = gateway.authenticate(agent.plaintext)
    assert actor is not None
    gateway.session_for(actor, session_id="s-1")
    assert TelemetryCapture(engine).events() == []
