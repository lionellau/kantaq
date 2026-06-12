"""The wire: streamable HTTP through the real SDK client (E09-T1, T1/T4/T7).

FakeMCPClient (MOD-30) is the official MCP client over in-process ASGI — the
full initialize handshake, session ids, tools/list, tools/call. Auth-edge
tests hit the ASGI app directly with Starlette's TestClient.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kantaq_core.identity import MintedToken
from kantaq_db.models import AuditEvent, Ticket
from kantaq_mcp.gateway import Gateway
from kantaq_mcp.server import build_gateway_app
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.mcp import FakeMCPClient

AuditProbe = Callable[..., list[AuditEvent]]


@pytest.fixture
def app_factory(gateway: Gateway) -> Callable[[], Starlette]:
    """A fresh app per client: a session manager's run() is single-use."""
    return lambda: build_gateway_app(gateway)


# ----------------------------------------------------------- auth at the door


def test_token_required_even_on_localhost(
    app_factory: Callable[[], Starlette], audit_rows: AuditProbe
) -> None:
    client = TestClient(app_factory())
    response = client.post("/v1/mcp", json={})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"

    bad = client.post("/v1/mcp", json={}, headers={"Authorization": "Bearer kq_nope.nope"})
    assert bad.status_code == 401

    denials = audit_rows("tool.deny")
    assert len(denials) == 2
    assert {row.actor_id for row in denials} == {"unknown"}
    assert all(row.after is not None and row.after["reason"] == "identity" for row in denials)


def test_browser_origins_are_rejected_before_the_token_is_read(
    app_factory: Callable[[], Starlette], owner: MintedToken
) -> None:
    client = TestClient(app_factory())
    response = client.post(
        "/v1/mcp",
        json={},
        headers={
            "Authorization": f"Bearer {owner.plaintext}",
            "Origin": "http://evil.example",
        },
    )
    assert response.status_code == 403


def test_healthz_stays_open(app_factory: Callable[[], Starlette]) -> None:
    response = TestClient(app_factory()).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ------------------------------------------------------------- the MCP wire


def test_connect_lists_the_session_allowlist(
    app_factory: Callable[[], Starlette],
    agent: MintedToken,
    viewer: MintedToken,
) -> None:
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        assert client.initialize_result is not None
        assert client.initialize_result.serverInfo.name == "kantaq-gateway"
        assert client.session_id, "stateful transport: the session id keys the gateway session"
        names = [tool.name for tool in client.list_tools().tools]
        assert names == ["ticket_get", "agent_action_propose"]

    with FakeMCPClient(app_factory(), token=viewer.plaintext) as client:
        names = [tool.name for tool in client.list_tools().tools]
        assert names == ["ticket_get"], "the allowlist is fixed by the member's scope"


def test_ticket_get_round_trip_over_the_wire(
    app_factory: Callable[[], Starlette], agent: MintedToken, ticket: Ticket
) -> None:
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        result = client.call_tool("ticket_get", {"ticket_id": ticket.id})
    assert not result.isError
    assert result.structuredContent is not None
    body = result.structuredContent["ticket"]
    assert body["id"] == ticket.id
    assert body["title"].startswith('<untrusted source="ticket.title">')


def test_propose_round_trip_persists_and_reports_not_applied(
    app_factory: Callable[[], Starlette],
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
) -> None:
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        result = client.call_tool(
            "agent_action_propose",
            {"ticket_id": ticket.id, "changes": {"status": "done"}, "note": "ship it"},
        )
    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["applied"] is False
    assert len(audit_rows("proposal.create")) == 1


def test_denied_call_comes_back_as_a_structured_error(
    app_factory: Callable[[], Starlette],
    viewer: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
) -> None:
    with FakeMCPClient(app_factory(), token=viewer.plaintext) as client:
        result = client.call_tool(
            "agent_action_propose", {"ticket_id": ticket.id, "changes": {"status": "done"}}
        )
    assert result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["error"]["code"] == "tool_allowlist"
    assert len(audit_rows("tool.deny")) == 1


def test_input_schema_is_enforced_on_the_wire(
    app_factory: Callable[[], Starlette], agent: MintedToken, ticket: Ticket
) -> None:
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        client.list_tools()  # prime the SDK's schema cache, as a real client does
        result = client.call_tool("ticket_get", {})
    assert result.isError


def test_session_expiry_over_the_wire(
    app_factory: Callable[[], Starlette],
    clock: FakeClock,
    agent: MintedToken,
    ticket: Ticket,
) -> None:
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        ok = client.call_tool("ticket_get", {"ticket_id": ticket.id})
        assert not ok.isError
        clock.advance(3601)
        expired = client.call_tool("ticket_get", {"ticket_id": ticket.id})
        assert expired.isError
        assert expired.structuredContent is not None
        assert expired.structuredContent["error"]["code"] == "expiry"

    # Re-initializing (a fresh transport session) restores service (T7).
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        again = client.call_tool("ticket_get", {"ticket_id": ticket.id})
        assert not again.isError


def test_lifespan_exit_flushes_aggregated_reads(
    app_factory: Callable[[], Starlette],
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
) -> None:
    with FakeMCPClient(app_factory(), token=agent.plaintext) as client:
        client.call_tool("ticket_get", {"ticket_id": ticket.id})
        assert audit_rows("agent.read") == []
    # Closing the client tears down the app lifespan, which flushes reads.
    rows = audit_rows("agent.read")
    assert len(rows) == 1
    assert rows[0].after is not None and rows[0].after["reads"] == 1
