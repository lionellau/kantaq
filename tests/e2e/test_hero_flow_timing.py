"""Hero-flow timing gate (E27-T3, MOD-15) — the kantaq-side flow, timed.

The v0.1 release gate (roadmap §2): the hero flow runs end to end in under 15
minutes. This wires the loop a fresh teammate walks —

    join → first project → connect an agent that reads a ticket over MCP and
    proposes a change → approve it from the human side → the signed change
    syncs to a second client

through ``HeroFlowTimer`` and asserts it stays under budget. Every step moves
through the real packages: the runtime app, the MCP gateway, the propose/approve
path, and the sync engine.

What is real vs scripted (be precise — kantaq runs no LLM; the agent is the
external, LLM-backed client):
  - REAL here: the whole kantaq side. ``FakeMCPClient`` is the *actual* MCP SDK
    client over real HTTP, so transport, bearer auth, session init, the tool
    catalog, scopes, audit, signed events, approval, and signed sync are all
    exercised.
  - SCRIPTED here: the agent's *decisions*. A human wrote "call ticket_get then
    agent_action_propose" instead of an LLM choosing them, so this gate is
    deterministic and offline (no model API, no flakiness, no cost).

A *real* LLM-backed agent (Claude Code / Codex) connecting, reading, and
proposing is verified separately by ``scripts/verify_agent.py``
(``make verify-agent``, recorded in docs/clients/compatibility.md) — opt-in,
since a real agent needs auth + network and is non-deterministic. The honest
wall-clock run with a real agent + real Supabase is the release-demo
measurement (exit criterion #1).

The gate's teeth are proven by ``test_hero_flow_gate_trips_when_slow`` — a gate
that cannot fail is worthless (the MOD-30 failing-fixture rule).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from kantaq_core.identity import IdentityService, Role, TokenVerifier, verification_roots
from kantaq_db import Workspace
from kantaq_db.models import CapabilityGrantRow, EventLog, Ticket
from kantaq_mcp.gateway import Gateway
from kantaq_mcp.server import build_gateway_app
from kantaq_protocol import verify
from kantaq_runtime.app import create_app
from kantaq_runtime.auth import ensure_device_identity, keychain_for
from kantaq_runtime.config import Settings
from kantaq_sync_engine import SyncEngine, row_to_event
from kantaq_test_harness import FakeMCPClient, HeroFlowTimer
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.hero_flow import DEFAULT_BUDGET_SECONDS, HeroFlowTooSlow

TEAMMATE_ACTOR = "mbr_teammate".ljust(26, "0")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _teammate_db(source: Engine, tmp_path: Path) -> Engine:
    """A second member's fresh runtime, sharing only the workspace baseline the
    team manifest distributes out of band (the rest arrives as events)."""
    db = create_engine(f"sqlite:///{tmp_path / 'teammate.sqlite'}")
    SQLModel.metadata.create_all(db)
    with Session(source) as src, Session(db) as dst:
        for workspace in src.exec(select(Workspace)).all():
            dst.add(Workspace(id=workspace.id, name=workspace.name))
        dst.commit()
    return db


def test_hero_flow_end_to_end_under_budget(engine: Engine, tmp_path: Path) -> None:
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"), sign_events=True)
    backend = FakeBackend()

    with HeroFlowTimer() as timer:  # default 15-minute budget
        # 1. A fresh member joins and runs locally: mint the owner token, invite
        # the agent, and provision the device key (the signing precondition).
        with Session(engine) as session:
            owner = IdentityService(session).bootstrap_owner()
            agent = IdentityService(session).invite(
                email="agent@local", role=Role.agent, scopes=["tickets.read", "proposals.write"]
            )
        assert owner is not None
        ensure_device_identity(engine, keychain_for(settings))

        runtime = create_app(
            settings=settings,
            engine=engine,
            verifier=TokenVerifier(engine, now=FakeClock().monotonic),
        )
        with TestClient(runtime) as client:
            # 2. First project — the onboarding wizard's call (E21-T3).
            project = client.post(
                "/v1/projects",
                json={"name": "Apollo", "goal": "Ship v0.1"},
                headers=_bearer(owner.plaintext),
            )
            assert project.status_code == 201, project.text
            project_id = project.json()["id"]

            # The member files a ticket for the agent to pick up.
            ticket = client.post(
                "/v1/tickets",
                json={"project_id": project_id, "title": "Wire the loopback gateway"},
                headers=_bearer(owner.plaintext),
            )
            assert ticket.status_code == 201, ticket.text
            ticket_id = ticket.json()["id"]

            # 3-4. Connect the agent and read + propose over MCP (scripted agent).
            gclock = FakeClock()
            gateway_app = build_gateway_app(
                Gateway(
                    engine, verifier=TokenVerifier(engine, now=gclock.monotonic), now=gclock.now
                )
            )
            with FakeMCPClient(gateway_app, token=agent.plaintext) as mcp:
                read = mcp.call_tool("ticket_get", {"ticket_id": ticket_id})
                assert not read.isError, read
                assert read.structuredContent["ticket"]["id"] == ticket_id
                proposed = mcp.call_tool(
                    "agent_action_propose",
                    {"ticket_id": ticket_id, "changes": {"status": "doing"}, "note": "starting"},
                )
                assert not proposed.isError, proposed
                # Propose-only: nothing applies until a human approves it.
                assert proposed.structuredContent["applied"] is False

            # 5. The human approves from the Inbox (commits the change, signs it,
            # and audits proposer and approver as distinct actors).
            pending = client.get(
                "/v1/proposals", params={"status": "pending"}, headers=_bearer(owner.plaintext)
            )
            assert pending.status_code == 200, pending.text
            proposal_id = pending.json()[0]["id"]
            approved = client.post(
                f"/v1/proposals/{proposal_id}/approve", headers=_bearer(owner.plaintext)
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["ticket"]["status"] == "doing"

        # 6. The signed change syncs to a second client. Identity roots
        # (devices/actors) travel via the team manifest, not the domain fold, so
        # the teammate pulls the domain collections the change touched.
        teammate_db = _teammate_db(engine, tmp_path)
        SyncEngine(engine, backend, actor_id=owner.member_id).push()
        teammate = SyncEngine(teammate_db, backend, actor_id=TEAMMATE_ACTOR)
        applied = sum(teammate.pull(collection=name).applied for name in ("projects", "tickets"))
        assert applied >= 1

    timer.assert_under_budget()

    # The teammate sees the approved change.
    with Session(teammate_db) as session:
        synced = session.get(Ticket, ticket_id)
    assert synced is not None
    assert synced.status == "doing"

    # Every synced ticket event is Ed25519-signed and verifies against the
    # issuing device's root key (roadmap §2 — every synced event signed).
    with Session(engine) as session:
        roots = verification_roots(session)
        events = [
            row_to_event(row)
            for row in session.exec(select(EventLog).where(EventLog.collection == "tickets")).all()
        ]
        grants = {
            event.policy_ref: session.get(CapabilityGrantRow, event.policy_ref) for event in events
        }
    assert events, "the approved change produced ticket events"
    for event in events:
        assert event.sig is not None, "a synced ticket event was unsigned"
        grant = grants[event.policy_ref]
        assert grant is not None
        assert verify(event, roots[grant.issuer])


def test_hero_flow_gate_trips_when_slow() -> None:
    """Failing-fixture proof: an over-budget run raises (MOD-30). Drive the
    timer with a clock that jumps past the 15-minute budget and confirm the
    gate refuses to pass — the same assertion the real flow relies on."""
    ticks = iter([0.0, DEFAULT_BUDGET_SECONDS + 1.0])
    with HeroFlowTimer(clock=lambda: next(ticks)) as timer:
        pass  # the injected clock simulates a flow that overran the budget
    with pytest.raises(HeroFlowTooSlow):
        timer.assert_under_budget()
