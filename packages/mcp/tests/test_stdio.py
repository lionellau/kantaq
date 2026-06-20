"""The stdio MCP transport: the deny matrix, audit, and round-trip over stdio.

E09-T4 / FR-E09-5 / MOD-08. stdio is a *transport*, not a new gateway: these
prove a denial over stdio is byte-for-byte the decision it is over HTTP, because
it is the same ``Gateway.handle_call`` — only the wire changes. The agent drives
the **real MCP SDK client** against the stdio-configured server over the SDK's
in-memory client↔server streams (``FakeStdioMCPClient``), so the protocol path
(initialize, tools/list, tools/call, structured errors) is real; one separate
subprocess smoke proves the actual stdin/stdout pipe + the ``kantaq mcp stdio``
entrypoint.

The deny matrix pre-registers a crafted ``GatewaySession`` under the stdio
session id (the stdio resolver returns an existing session unchanged), so every
one of the eight checks can be made to fail over the wire — the same technique
the gate-suite uses for the gateway's own checks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from mcp.types import CallToolResult
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import MintedToken
from kantaq_core.memory.service import MemoryService
from kantaq_db.models import AuditEvent, Ticket
from kantaq_mcp.gateway import (
    DENY_AUDIT_POLICY,
    DENY_COLLECTION_SCOPE,
    DENY_IDENTITY,
    DENY_MEMORY_POLICY,
    DENY_RATE_LIMIT,
    DENY_TOOL_ALLOWLIST,
    DENY_VERB_MATCH,
    DENY_WRITE_MODE,
    Gateway,
)
from kantaq_mcp.session import (
    AUDIT_POLICY_STANDARD,
    RATE_LIMIT_PER_SESSION,
    WRITE_MODE_PROPOSE_ONLY,
    GatewaySession,
)
from kantaq_mcp.stdio import STDIO_SESSION_ID, StdioAuthError, StdioCredentials, serve_stdio
from kantaq_mcp.stdio import build_stdio_server as _build
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.mcp import FakeStdioMCPClient

# ----------------------------------------------------------------- helpers


def _client(gateway: Gateway, token: str | None) -> FakeStdioMCPClient:
    """A stdio client whose env-supplied bearer is ``token`` (the resolver
    re-verifies it per call, exactly as the real ``kantaq mcp stdio`` does)."""
    return FakeStdioMCPClient(_build(gateway, StdioCredentials(token=token)))


def _error_code(result: CallToolResult) -> str:
    """The structured deny/​error code a tools/call returned over the wire."""
    assert result.isError, "expected a structured error result"
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    code: str = payload["error"]["code"]
    return code


def _craft(
    member_id: str,
    now: datetime,
    *,
    allowed_tools: tuple[str, ...],
    granted_verbs: tuple[str, ...],
    write_mode: str = WRITE_MODE_PROPOSE_ONLY,
    collection_scope: tuple[str, ...] = ("*",),
    audit_policy: str = AUDIT_POLICY_STANDARD,
    role: str = "Owner",
    agent_role: str | None = None,
    expires_at: datetime | None = None,
    calls_total: int = 0,
) -> GatewaySession:
    """A gateway session pinned to the stdio session id, crafted to fail one
    check (the gate-suite technique) so the deny is exercised over the real wire."""
    return GatewaySession(
        session_id=STDIO_SESSION_ID,
        member_id=member_id,
        role=role,
        token_id="stdio-test",
        scopes=granted_verbs,
        allowed_tools=allowed_tools,
        write_mode=write_mode,
        created_at=now,
        expires_at=expires_at or (now + timedelta(hours=1)),
        collection_scope=collection_scope,
        granted_verbs=granted_verbs,
        agent_role=agent_role,
        audit_policy=audit_policy,
        calls_total=calls_total,
    )


def _now(clock: FakeClock) -> datetime:
    return clock.now().replace(tzinfo=None)


# ----------------------------------------------------------------- round-trip


def test_wire_round_trip_over_stdio(gateway: Gateway, owner: MintedToken, ticket: Ticket) -> None:
    """initialize → tools/list → a real tool call → a structured error, over the
    SDK's stdio client. The owner's token-derived session lists the catalog."""
    with _client(gateway, owner.plaintext) as client:
        tools = {t.name for t in client.list_tools().tools}
        assert "ticket_get" in tools and "agent_action_propose" in tools

        ok = client.call_tool("ticket_get", {"ticket_id": ticket.id})
        assert not ok.isError
        assert ok.structuredContent is not None
        assert ok.structuredContent["ticket"]["id"] == ticket.id

        # A tool the catalog never had is a structured deny, not a crash.
        assert _error_code(client.call_tool("ticket_update", {"ticket_id": ticket.id})) == (
            DENY_TOOL_ALLOWLIST
        )


def test_missing_or_bad_token_denies_identity_over_stdio(
    gateway: Gateway,
    owner: MintedToken,
    ticket: Ticket,
    audit_rows: Callable[..., list[AuditEvent]],
) -> None:
    """No middleware over a pipe: the resolver re-verifies the token per call, so
    a bad/absent token is an audited ``identity`` deny and an empty tools/list."""
    with _client(gateway, "kq_not.a_real_token") as client:
        assert client.list_tools().tools == []  # fail closed: no tools for no identity
        denied = client.call_tool("ticket_get", {"ticket_id": ticket.id})
        assert _error_code(denied) == DENY_IDENTITY
    assert any(r.action == "tool.deny" for r in audit_rows("tool.deny"))


# ----------------------------------------------------------------- deny matrix


def test_deny_matrix_over_stdio(
    gateway: Gateway,
    engine: Engine,
    owner: MintedToken,
    ticket: Ticket,
    clock: FakeClock,
    audit_rows: Callable[..., list[AuditEvent]],
    table_counts: Callable[[], dict[str, int]],
) -> None:
    """Every gateway check, made to fail over the stdio wire, returns its
    structured deny, writes exactly one ``tool.deny``, and changes nothing — the
    full permission-denial matrix, byte-for-byte the HTTP decision."""
    now = _now(clock)
    member = owner.member_id
    # A real team entry so the role-less-agent memory read hits the policy gate
    # (a non-existent id would be not_found, before the policy check).
    with Session(engine) as session:
        mem_id = (
            MemoryService(session, actor_id=member, source="app", now=clock.now)
            .create_entry(title="t", body="b", space="codebase", visibility="team")
            .id
        )

    # (crafted session, tool, args, expected deny reason)
    cases: list[tuple[GatewaySession, str, dict[str, object], str]] = [
        # collection scope: a memory-only scope reaching into tickets.
        (
            _craft(
                member,
                now,
                allowed_tools=("ticket_get",),
                granted_verbs=("tickets.read",),
                collection_scope=("memory_entries",),
            ),
            "ticket_get",
            {"ticket_id": ticket.id},
            DENY_COLLECTION_SCOPE,
        ),
        # tool allowlist: a tool not in the session's fixed set.
        (
            _craft(member, now, allowed_tools=("ticket_get",), granted_verbs=("tickets.read",)),
            "agent_action_propose",
            {"ticket_id": ticket.id, "changes": {"status": "done"}},
            DENY_TOOL_ALLOWLIST,
        ),
        # verb match: the tool is in the allowlist but its action is not granted.
        (
            _craft(
                member,
                now,
                allowed_tools=("ticket_get", "agent_action_propose"),
                granted_verbs=("tickets.read",),  # no proposals.write
            ),
            "agent_action_propose",
            {"ticket_id": ticket.id, "changes": {"status": "done"}},
            DENY_VERB_MATCH,
        ),
        # write mode / apply verb (DEBT-37): an over-scoped session still cannot
        # approve over stdio — approve needs direct_write, which nothing holds.
        (
            _craft(
                member,
                now,
                allowed_tools=("agent_action_approve",),
                granted_verbs=("tickets.write",),
                write_mode=WRITE_MODE_PROPOSE_ONLY,
            ),
            "agent_action_approve",
            {"proposal_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ"},
            DENY_WRITE_MODE,
        ),
        # expiry: a session past its window only denies.
        (
            _craft(
                member,
                now,
                allowed_tools=("ticket_get",),
                granted_verbs=("tickets.read",),
                expires_at=now - timedelta(seconds=1),
            ),
            "ticket_get",
            {"ticket_id": ticket.id},
            "expiry",
        ),
        # rate limit: at the per-session ceiling, the next call kills the session.
        (
            _craft(
                member,
                now,
                allowed_tools=("ticket_get",),
                granted_verbs=("tickets.read",),
                calls_total=RATE_LIMIT_PER_SESSION,
            ),
            "ticket_get",
            {"ticket_id": ticket.id},
            DENY_RATE_LIMIT,
        ),
        # audit policy: a session whose audit policy is unknown cannot be audited.
        (
            _craft(
                member,
                now,
                allowed_tools=("ticket_get",),
                granted_verbs=("tickets.read",),
                audit_policy="bogus",
            ),
            "ticket_get",
            {"ticket_id": ticket.id},
            DENY_AUDIT_POLICY,
        ),
        # memory policy on reads: a role-less agent may not read memory at all.
        (
            _craft(
                member,
                now,
                allowed_tools=("memory_get",),
                granted_verbs=("memory.read",),
                role="Agent",
                agent_role=None,
            ),
            "memory_get",
            {"memory_id": mem_id},
            DENY_MEMORY_POLICY,
        ),
    ]

    for crafted, tool, args, reason in cases:
        before_counts = table_counts()
        before_denials = len(audit_rows("tool.deny"))
        gateway.sessions.put(crafted)  # the stdio resolver returns this unchanged
        with _client(gateway, owner.plaintext) as client:
            result = client.call_tool(tool, args)
        assert _error_code(result) == reason, f"{tool}: wrong deny reason"
        after = audit_rows("tool.deny")
        assert len(after) == before_denials + 1, f"{reason}: expected exactly one tool.deny"
        assert after[-1].after["reason"] == reason
        assert after[-1].actor_id == member  # attributed to the acting member
        assert table_counts() == before_counts, f"{reason}: a denial changed state"


# ----------------------------------------------------------------- audit


def test_reads_aggregate_and_denials_detail_over_stdio(
    gateway: Gateway,
    owner: MintedToken,
    ticket: Ticket,
    audit_rows: Callable[..., list[AuditEvent]],
) -> None:
    """Audit completeness over stdio: agent reads aggregate to one ``agent.read``
    summary (flushed at shutdown), a denial is its own detailed ``tool.deny``."""
    with _client(gateway, owner.plaintext) as client:
        for _ in range(3):
            assert not client.call_tool("ticket_get", {"ticket_id": ticket.id}).isError
        assert client.call_tool("ticket_get", {"ticket_id": "01JMISSINGZZZZZZZZZZZZZZZZ"}).isError
    # Reads flush at lifespan/loop shutdown; force it (the CLI does this in finally).
    gateway.flush_reads()

    reads = audit_rows("agent.read")
    assert len(reads) == 1, "three reads must aggregate into one summary row"
    assert reads[0].actor_id == owner.member_id
    # The missing-ticket read is a ToolError (not a gateway deny) — it still
    # aggregates as a read; the point is reads summarize, they do not detail.
    assert all(r.actor_id == owner.member_id for r in audit_rows())


# ----------------------------------------------------------------- startup auth


def test_serve_stdio_fails_closed_without_a_token(
    gateway: Gateway, owner: MintedToken, audit_rows: Callable[..., list[AuditEvent]]
) -> None:
    """``kantaq mcp stdio`` refuses to come up on a missing/invalid token — a
    clear, audited startup failure (the parent misconfigured the env), never a
    process that serves and silently denies. No socket is ever bound."""
    with pytest.raises(StdioAuthError):
        serve_stdio(gateway, StdioCredentials(token=None))
    with pytest.raises(StdioAuthError):
        serve_stdio(gateway, StdioCredentials(token="kq_bogus.token"))
    assert any(r.action == "tool.deny" and r.after["reason"] == DENY_IDENTITY for r in audit_rows())


def test_stdio_credentials_from_env_reads_the_contract() -> None:
    """The env-var grant-binding contract the compat harness aligns to."""
    creds = StdioCredentials.from_env(
        {
            "KANTAQ_MCP_TOKEN": " kq_a.b ",
            "KANTAQ_MCP_GRANT_ID": "grt_1",
            "KANTAQ_MCP_AGENT_ROLE": "code_agent",
        }
    )
    assert creds.token == "kq_a.b"  # trimmed
    req = creds.grant_request()
    assert req is not None and req.grant_id == "grt_1" and req.agent_role == "code_agent"
    # Token-only: no grant binding.
    assert StdioCredentials.from_env({"KANTAQ_MCP_TOKEN": "kq_a.b"}).grant_request() is None
    assert StdioCredentials.from_env({}).token is None
