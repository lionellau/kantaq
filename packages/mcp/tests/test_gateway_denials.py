"""The permission-denial matrix (MOD-08 required tests, NFR-E09-1).

Every gateway check fails closed, changes nothing, and writes a ``tool.deny``
audit row naming the failed check. The "changes nothing" probe counts the
rows a successful call would have touched (tickets, proposals, event log).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kantaq_core.identity import MintedToken, VerifiedActor
from kantaq_db.models import AuditEvent, Ticket
from kantaq_mcp.gateway import (
    DENY_EXPIRY,
    DENY_IDENTITY,
    DENY_RATE_LIMIT,
    DENY_TOOL_ALLOWLIST,
    DENY_WRITE_MODE,
    Gateway,
    GatewayDenied,
)
from kantaq_mcp.session import (
    RATE_LIMIT_PER_SESSION,
    WRITE_MODE_READ_ONLY,
    GatewaySession,
)
from kantaq_test_harness.clock import FakeClock

AuditProbe = Callable[..., list[AuditEvent]]
CountProbe = Callable[[], dict[str, int]]


def _verified(gateway: Gateway, minted: MintedToken) -> VerifiedActor:
    actor = gateway.authenticate(minted.plaintext)
    assert actor is not None
    return actor


def _assert_denied(
    gateway: Gateway,
    *,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
    actor: VerifiedActor,
    session: GatewaySession,
    tool: str,
    args: dict[str, Any],
    reason: str,
) -> None:
    """Assert the call is denied, applies nothing, and audits the denial."""
    before = table_counts()
    denials_before = len(audit_rows("tool.deny"))
    with pytest.raises(GatewayDenied) as denied:
        gateway.handle_call(actor=actor, session=session, tool_name=tool, args=dict(args))
    assert denied.value.reason == reason
    assert table_counts() == before, "a denied call must apply nothing"
    denials = audit_rows("tool.deny")
    assert len(denials) == denials_before + 1
    row = denials[-1]
    assert row.source == "mcp"
    assert row.after is not None and row.after["reason"] == reason


def test_identity_check_token_must_match_the_session(
    gateway: Gateway,
    agent: MintedToken,
    viewer: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    agent_actor = _verified(gateway, agent)
    session = gateway.session_for(agent_actor, session_id="s-agent")
    intruder = _verified(gateway, viewer)
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=intruder,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_IDENTITY,
    )
    # The denial is attributed to the session's member, with the failed check.
    assert audit_rows("tool.deny")[-1].actor_id == session.member_id


def test_expiry_check_denies_and_keeps_denying(
    gateway: Gateway,
    clock: FakeClock,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    actor = _verified(gateway, agent)
    session = gateway.session_for(actor, session_id="s-agent")
    clock.advance(3601)
    for _ in range(2):  # expired sessions never silently renew
        _assert_denied(
            gateway,
            audit_rows=audit_rows,
            table_counts=table_counts,
            actor=actor,
            session=session,
            tool="ticket_get",
            args={"ticket_id": ticket.id},
            reason=DENY_EXPIRY,
        )


def test_tool_allowlist_denies_out_of_scope_and_unknown_tools(
    gateway: Gateway,
    readonly_agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    actor = _verified(gateway, readonly_agent)
    session = gateway.session_for(actor, session_id="s-ro")
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="agent_action_propose",
        args={"ticket_id": ticket.id, "changes": {"status": "done"}},
        reason=DENY_TOOL_ALLOWLIST,
    )
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="permission_grant",  # the PRD's "never in the allowlist" example
        args={},
        reason=DENY_TOOL_ALLOWLIST,
    )


def test_write_mode_check_denies_propose_even_if_allowlisted(
    gateway: Gateway,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    """Defense in depth: a session that somehow carries a propose tool with a
    read_only write mode still fails the verb check."""
    actor = _verified(gateway, agent)
    session = gateway.session_for(actor, session_id="s-agent")
    session.write_mode = WRITE_MODE_READ_ONLY
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="agent_action_propose",
        args={"ticket_id": ticket.id, "changes": {"status": "done"}},
        reason=DENY_WRITE_MODE,
    )


def test_rate_limit_kills_the_session_and_audits(
    gateway: Gateway,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    actor = _verified(gateway, agent)
    session = gateway.session_for(actor, session_id="s-agent")
    for _ in range(50):
        gateway.handle_call(
            actor=actor, session=session, tool_name="ticket_get", args={"ticket_id": ticket.id}
        )
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_RATE_LIMIT,
    )
    assert session.killed
    # Killed is sticky: the next call is denied before anything else runs.
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_RATE_LIMIT,
    )


def test_session_lifetime_cap_kills_too(
    gateway: Gateway,
    clock: FakeClock,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    actor = _verified(gateway, agent)
    session = gateway.session_for(actor, session_id="s-agent")
    session.calls_total = RATE_LIMIT_PER_SESSION
    clock.advance(61)  # fresh minute window: only the lifetime cap can trip
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_RATE_LIMIT,
    )
