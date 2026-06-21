"""HTTP and stdio resolve to the same session + the same checks (E09-T5 / D-34).

The dual-transport contract behind the snippet generator listing both endpoints:
a token produces the SAME gateway session whether the credential arrives in an
HTTP request header or a stdio env var, and the eight checks then decide
identically. Both transports' real resolvers — ``_request_actor_session``
(HTTP) and ``_StdioActorSession`` (stdio) — extract ``(session_id,
grant_request)`` from their wire and call the one shared primitive
``gateway.session_for``; this pins that they cannot drift. Distinct session ids
are used on purpose so each side is a genuine derivation, not one cached object.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from starlette.requests import Request

from kantaq_core.identity import MintedToken
from kantaq_db.models import Ticket
from kantaq_mcp.gateway import Gateway, GatewayDenied
from kantaq_mcp.server import (
    _ACTOR_SCOPE_KEY,
    _request_actor_session,
)
from kantaq_mcp.session import GatewaySession
from kantaq_mcp.stdio import StdioCredentials, _StdioActorSession

# The session fields a transport must NOT change. session_id, created_at, and the
# mutable rate/kill counters are excluded — they are runtime state, not derivation.
_DERIVATION_FIELDS = (
    "member_id",
    "role",
    "token_id",
    "scopes",
    "allowed_tools",
    "write_mode",
    "expires_at",
    "collection_scope",
    "granted_verbs",
    "agent_role",
    "memory_policy_id",
    "audit_policy",
    "grant_id",
)


def _shape(session: GatewaySession) -> dict[str, Any]:
    return {field: getattr(session, field) for field in _DERIVATION_FIELDS}


def _http_resolve(gateway: Gateway, actor: Any, *, session_id: str) -> tuple[Any, GatewaySession]:
    """Drive the REAL HTTP resolver with a request the middleware would have built."""
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"mcp-session-id", session_id.encode())],
            "state": {_ACTOR_SCOPE_KEY: actor},
        }
    )
    server = SimpleNamespace(request_context=SimpleNamespace(request=request))
    return _request_actor_session(server, gateway)


def _stdio_resolve(
    gateway: Gateway, plaintext: str, *, session_id: str
) -> tuple[Any, GatewaySession]:
    """Drive the REAL stdio resolver with env-supplied credentials."""
    creds = StdioCredentials(token=plaintext)
    return _StdioActorSession(gateway, creds, session_id=session_id)(SimpleNamespace())


def test_token_session_is_identical_over_http_and_stdio(
    gateway: Gateway, agent: MintedToken
) -> None:
    actor = gateway.authenticate(agent.plaintext)
    http_actor, http_session = _http_resolve(gateway, actor, session_id="http-tok")
    stdio_actor, stdio_session = _stdio_resolve(gateway, agent.plaintext, session_id="stdio-tok")

    assert http_actor.member_id == stdio_actor.member_id
    # Two distinct derivations (different session ids), identical in every
    # derivation field — the transport changed nothing.
    assert http_session.session_id != stdio_session.session_id
    assert _shape(http_session) == _shape(stdio_session)


def test_an_allowed_read_returns_identically_over_both_transports(
    gateway: Gateway, agent: MintedToken, ticket: Ticket
) -> None:
    actor = gateway.authenticate(agent.plaintext)
    _, http_session = _http_resolve(gateway, actor, session_id="http-read")
    _, stdio_session = _stdio_resolve(gateway, agent.plaintext, session_id="stdio-read")

    http_result = gateway.handle_call(
        actor=actor, session=http_session, tool_name="ticket_get", args={"ticket_id": ticket.id}
    )
    stdio_result = gateway.handle_call(
        actor=actor, session=stdio_session, tool_name="ticket_get", args={"ticket_id": ticket.id}
    )
    assert http_result == stdio_result


def test_a_denied_apply_denies_identically_over_both_transports(
    gateway: Gateway, agent: MintedToken
) -> None:
    """agent_action_approve is an apply verb the gateway never issues a session
    for — both transports must deny it with the same structured reason."""
    actor = gateway.authenticate(agent.plaintext)
    _, http_session = _http_resolve(gateway, actor, session_id="http-deny")
    _, stdio_session = _stdio_resolve(gateway, agent.plaintext, session_id="stdio-deny")

    http_denied: GatewayDenied | None = None
    stdio_denied: GatewayDenied | None = None
    try:
        gateway.handle_call(
            actor=actor,
            session=http_session,
            tool_name="agent_action_approve",
            args={"proposal_id": "x"},
        )
    except GatewayDenied as exc:
        http_denied = exc
    try:
        gateway.handle_call(
            actor=actor,
            session=stdio_session,
            tool_name="agent_action_approve",
            args={"proposal_id": "x"},
        )
    except GatewayDenied as exc:
        stdio_denied = exc

    assert http_denied is not None and stdio_denied is not None
    assert http_denied.reason == stdio_denied.reason
