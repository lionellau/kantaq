"""Grant-derived sessions and the v0.1 eight checks (E09-T3, MOD-08/MOD-06).

Extends the v0.0.5 denial matrix (``test_gateway_denials``) with everything the
capability grant adds: session derivation from a grant (scope, tools, write
mode, memory policy, expiry), the new per-call checks (collection scope, verb
match, audit policy), the live grant re-check that makes revocation stop a
derived session (NFR-E06-2), and the ``/v1/session/init`` descriptor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session
from starlette.testclient import TestClient

from kantaq_core.identity import GrantService, MintedToken, VerifiedActor, ensure_device
from kantaq_db.models import AuditEvent, Ticket
from kantaq_mcp.catalog import CATALOG
from kantaq_mcp.gateway import (
    DENY_AUDIT_POLICY,
    DENY_COLLECTION_SCOPE,
    DENY_IDENTITY,
    DENY_VERB_MATCH,
    Gateway,
    GatewayDenied,
    GrantSessionRequest,
)
from kantaq_mcp.server import build_gateway_app
from kantaq_mcp.session import WRITE_MODE_PROPOSE_ONLY, GatewaySession
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.keychain import FakeKeychain

AuditProbe = Callable[..., list[AuditEvent]]
CountProbe = Callable[[], dict[str, int]]


@pytest.fixture
def keychain() -> FakeKeychain:
    return FakeKeychain()


def _verified(gateway: Gateway, minted: MintedToken) -> VerifiedActor:
    actor = gateway.authenticate(minted.plaintext)
    assert actor is not None
    return actor


def _naive(clock: FakeClock) -> Callable[[], datetime]:
    return lambda: clock.now().replace(tzinfo=None)


def _tools_for(*actions: str) -> set[str]:
    return {spec.name for spec in CATALOG if spec.required_action in actions}


def _issue_grant(
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    *,
    subject_member_id: str,
    issuer_member_id: str,
    resource: str = "workspace/main",
    verbs: tuple[str, ...] = ("tickets.read", "proposals.write"),
) -> str:
    """Boot the runtime device and issue a signed grant for ``subject``."""
    with Session(engine) as session:
        ensure_device(session, keychain, member_id=issuer_member_id, now=_naive(clock)())
        session.commit()
        row = GrantService(session, keychain, now=_naive(clock)).issue(
            subject_member_id=subject_member_id,
            resource=resource,
            verbs=list(verbs),
            actor_id=issuer_member_id,
        )
        session.commit()
        return row.id


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
    before = table_counts()
    denials_before = len(audit_rows("tool.deny"))
    with pytest.raises(GatewayDenied) as denied:
        gateway.handle_call(actor=actor, session=session, tool_name=tool, args=dict(args))
    assert denied.value.reason == reason
    assert table_counts() == before, "a denied call must apply nothing"
    denials = audit_rows("tool.deny")
    assert len(denials) == denials_before + 1
    assert denials[-1].after is not None and denials[-1].after["reason"] == reason


# ----------------------------------------------------- grant-derived session


def test_grant_session_derives_scope_tools_writemode_expiry_policy(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
) -> None:
    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    actor = _verified(gateway, agent)
    session = gateway.session_for(
        actor,
        session_id="s-grant",
        grant_request=GrantSessionRequest(grant_id=grant_id, agent_role="code_agent"),
    )
    assert session.grant_id == grant_id
    assert set(session.allowed_tools) == _tools_for("tickets.read", "proposals.write")
    assert session.write_mode == WRITE_MODE_PROPOSE_ONLY
    assert session.collection_scope == ("*",)  # workspace/main -> all collections
    assert session.agent_role == "code_agent"
    assert session.memory_policy_id == "memory-policy/code_agent/v1"
    # Expiry is the grant's own (1 h default from the FakeClock epoch).
    assert session.expires_at == datetime(2026, 1, 1, 1, 0, 0)
    # The same transport id returns the same session (no re-derivation/escalation).
    assert gateway.session_for(actor, session_id="s-grant") is session


def test_read_only_grant_yields_a_read_only_session(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    readonly_agent: MintedToken,
    owner: MintedToken,
) -> None:
    grant_id = _issue_grant(
        engine,
        keychain,
        clock,
        subject_member_id=readonly_agent.member_id,
        issuer_member_id=owner.member_id,
        verbs=("tickets.read",),
    )
    actor = _verified(gateway, readonly_agent)
    session = gateway.session_for(
        actor, session_id="s-ro", grant_request=GrantSessionRequest(grant_id=grant_id)
    )
    assert set(session.allowed_tools) == _tools_for("tickets.read")
    assert session.write_mode == "read_only"
    assert session.agent_role is None and session.memory_policy_id is None


# ------------------------------------------------- the new per-call checks


def test_collection_scope_denies_a_tool_outside_the_grant_resource(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    # A grant scoped to the tickets collection only; propose touches agent_proposals.
    grant_id = _issue_grant(
        engine,
        keychain,
        clock,
        subject_member_id=agent.member_id,
        issuer_member_id=owner.member_id,
        resource="tickets",
    )
    actor = _verified(gateway, agent)
    session = gateway.session_for(
        actor, session_id="s-scope", grant_request=GrantSessionRequest(grant_id=grant_id)
    )
    assert session.collection_scope == ("tickets",)
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="agent_action_propose",
        args={"ticket_id": ticket.id, "changes": {"status": "done"}},
        reason=DENY_COLLECTION_SCOPE,
    )
    # ...but a tickets-only tool still works under the same scope.
    result = gateway.handle_call(
        actor=actor, session=session, tool_name="ticket_get", args={"ticket_id": ticket.id}
    )
    assert "ticket" in result


def test_verb_match_denies_when_the_grant_lacks_the_capability(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    """Defense in depth: an allowlisted tool whose verb left the grant still denies."""
    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    actor = _verified(gateway, agent)
    session = gateway.session_for(
        actor, session_id="s-verb", grant_request=GrantSessionRequest(grant_id=grant_id)
    )
    session.granted_verbs = ()  # the grant's verb set drifted from the allowlist
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_VERB_MATCH,
    )


def test_audit_policy_denies_an_unknown_policy(
    gateway: Gateway,
    agent: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    actor = _verified(gateway, agent)
    session = gateway.session_for(actor, session_id="s-audit")
    session.audit_policy = "bogus"  # nothing can be audited under an unknown policy
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_AUDIT_POLICY,
    )


def test_bad_agent_role_refuses_to_derive_a_session(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
) -> None:
    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    actor = _verified(gateway, agent)
    with pytest.raises(GatewayDenied) as denied:
        gateway.session_for(
            actor,
            session_id="s-bad",
            grant_request=GrantSessionRequest(grant_id=grant_id, agent_role="not_a_role"),
        )
    assert denied.value.reason == DENY_IDENTITY


# ----------------------------------------------- revocation stops the session


def test_revoking_the_grant_stops_the_derived_session(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
    ticket: Ticket,
    audit_rows: AuditProbe,
    table_counts: CountProbe,
) -> None:
    """NFR-E06-2: a revoked grant invalidates its derived session at once (< 5 s)."""
    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    actor = _verified(gateway, agent)
    session = gateway.session_for(
        actor, session_id="s-rev", grant_request=GrantSessionRequest(grant_id=grant_id)
    )
    # Live grant: the call goes through.
    ok = gateway.handle_call(
        actor=actor, session=session, tool_name="ticket_get", args={"ticket_id": ticket.id}
    )
    assert "ticket" in ok

    with Session(engine) as db:
        GrantService(db, keychain, now=_naive(clock)).revoke(grant_id, actor_id=owner.member_id)
        db.commit()

    clock.advance(4)  # well inside the 5 s budget; the store read sees it at once
    _assert_denied(
        gateway,
        audit_rows=audit_rows,
        table_counts=table_counts,
        actor=actor,
        session=session,
        tool="ticket_get",
        args={"ticket_id": ticket.id},
        reason=DENY_IDENTITY,
    )


def test_revocation_stops_the_session_within_the_wall_clock_budget(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
    ticket: Ticket,
) -> None:
    """E06-T7 / NFR-E06-2 — the timed proof, measured on a **real wall clock**.

    The unforgiving promise: a revoked grant stops its derived MCP session in
    under 5 s. The gateway re-checks the grant **live on every call** against the
    store (``_grant_live`` — no cached session that ignores revocation, which
    would be a security hole, not a latency miss), so the stop lands on the very
    next call. We measure with ``time.monotonic`` (not FakeClock) and assert the
    real elapsed wall-clock from revoke → denial is well inside the budget. This
    is the hermetic gate for the same-store path; the cross-replica propagation
    (revoke on the backend → the 2 s sync poll lands the revoked grant → the
    gateway's live re-check denies — D-21) is the live-Supabase smoke.
    """
    import time

    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    actor = _verified(gateway, agent)
    session = gateway.session_for(
        actor, session_id="s-wall", grant_request=GrantSessionRequest(grant_id=grant_id)
    )
    assert "ticket" in gateway.handle_call(
        actor=actor, session=session, tool_name="ticket_get", args={"ticket_id": ticket.id}
    )

    with Session(engine) as db:
        GrantService(db, keychain, now=_naive(clock)).revoke(grant_id, actor_id=owner.member_id)
        db.commit()

    revoked_at = time.monotonic()
    with pytest.raises(GatewayDenied) as denied:
        gateway.handle_call(
            actor=actor, session=session, tool_name="ticket_get", args={"ticket_id": ticket.id}
        )
    elapsed = time.monotonic() - revoked_at
    assert denied.value.reason == DENY_IDENTITY
    assert elapsed < 5.0, f"revocation took {elapsed:.4f}s wall-clock — over the 5 s budget"


# ------------------------------------------------------- /v1/session/init


def test_session_init_descriptor_describes_the_grant_session(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
) -> None:
    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    actor = _verified(gateway, agent)
    desc = gateway.describe_grant_session(
        actor, GrantSessionRequest(grant_id=grant_id, agent_role="qa_agent")
    )
    assert desc["grant_id"] == grant_id
    assert desc["agent_role"] == "qa_agent"
    assert set(desc["allowed_tools"]) == _tools_for("tickets.read", "proposals.write")
    assert desc["write_mode"] == "propose_only"
    assert desc["connect_headers"] == {"mcp-grant-id": grant_id, "mcp-agent-role": "qa_agent"}


def test_session_init_http_endpoint(
    gateway: Gateway,
    engine: Engine,
    keychain: FakeKeychain,
    clock: FakeClock,
    agent: MintedToken,
    owner: MintedToken,
) -> None:
    grant_id = _issue_grant(
        engine, keychain, clock, subject_member_id=agent.member_id, issuer_member_id=owner.member_id
    )
    client = TestClient(build_gateway_app(gateway))
    auth = {"Authorization": f"Bearer {agent.plaintext}"}

    assert client.post("/v1/session/init", json={"grant_id": grant_id}).status_code == 401

    ok = client.post(
        "/v1/session/init", json={"grant_id": grant_id, "agent_role": "code_agent"}, headers=auth
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["grant_id"] == grant_id
    assert body["agent_role"] == "code_agent"
    assert "instructions" in body

    bad = client.post("/v1/session/init", json={"grant_id": "grb_nope"}, headers=auth)
    assert bad.status_code == 403
