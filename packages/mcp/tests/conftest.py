"""Shared fixtures for the MOD-08/MOD-09 Gateway/Agent profile tests.

Everything is hermetic: temp SQLite, FakeClock-driven gateway and verifier,
identities minted through the real IdentityService, tracker rows written
through the real TrackerService. The MCP wire tests drive the gateway app
through FakeMCPClient (the official SDK client over in-process ASGI).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService, MintedToken, Role, TokenVerifier
from kantaq_core.tracker.service import TrackerService
from kantaq_db.models import AgentProposal, AuditEvent, Ticket, Workspace
from kantaq_db.models import EventLog as EventLogRow
from kantaq_mcp.gateway import Gateway
from kantaq_test_harness.clock import FakeClock

AGENT_SCOPES = ["tickets.read", "proposals.write"]
READONLY_SCOPES = ["tickets.read"]


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gateway(engine: Engine, clock: FakeClock) -> Gateway:
    return Gateway(
        engine,
        verifier=TokenVerifier(engine, now=clock.monotonic),
        now=clock.now,
    )


@pytest.fixture
def owner(engine: Engine) -> MintedToken:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted


@pytest.fixture
def agent(engine: Engine, owner: MintedToken) -> MintedToken:
    """An Agent member whose token may read tickets and propose changes."""
    with Session(engine) as session:
        return IdentityService(session).invite(
            email="agent@local", role=Role.agent, scopes=AGENT_SCOPES
        )


@pytest.fixture
def readonly_agent(engine: Engine, owner: MintedToken) -> MintedToken:
    """An Agent member whose token may only read tickets."""
    with Session(engine) as session:
        return IdentityService(session).invite(
            email="readonly-agent@local", role=Role.agent, scopes=READONLY_SCOPES
        )


@pytest.fixture
def viewer(engine: Engine, owner: MintedToken) -> MintedToken:
    with Session(engine) as session:
        return IdentityService(session).invite(email="viewer@local", role=Role.viewer)


@pytest.fixture
def ticket(engine: Engine, owner: MintedToken, clock: FakeClock) -> Ticket:
    """A seeded workspace + project + ticket, written like the runtime writes."""
    with Session(engine) as session:
        workspace = Workspace(name="kantaq")
        session.add(workspace)
        session.commit()
        service = TrackerService(session, actor_id=owner.member_id, source="app", now=clock.now)
        project = service.create_project(workspace_id=workspace.id, name="Sprint 2")
        return service.create_ticket(
            project_id=project.id,
            title="Wire the loopback gateway",
            description="Agents read tickets through their own loopback gateway.",
            labels=["mcp", "security"],
        )


@pytest.fixture
def audit_rows(engine: Engine) -> Callable[..., list[AuditEvent]]:
    """Read-side audit probe: all rows, oldest first, optionally by action."""

    def _rows(action: str | None = None) -> list[AuditEvent]:
        with Session(engine) as session:
            rows = sorted(session.exec(select(AuditEvent)).all(), key=lambda r: r.id)
        if action is not None:
            rows = [row for row in rows if row.action == action]
        return rows

    return _rows


@pytest.fixture
def table_counts(engine: Engine) -> Callable[[], dict[str, int]]:
    """The 'a denial changes nothing' probe (NFR-E09-1)."""

    def _counts() -> dict[str, int]:
        with Session(engine) as session:
            return {
                "tickets": len(session.exec(select(Ticket)).all()),
                "agent_proposals": len(session.exec(select(AgentProposal)).all()),
                "event_log": len(session.exec(select(EventLogRow)).all()),
            }

    return _counts
