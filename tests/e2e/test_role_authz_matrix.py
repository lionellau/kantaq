"""Role-based authorization, end to end over the runtime API (NFR-E06-3).

The CI gate behind `scripts/uat_roles.py` (the human-readable reporter). Where
`test_roles.py` unit-tests the `can()` matrix, this proves the matrix is actually
*enforced on the web/API surface*: every base role (Owner/Maintainer/Member/Viewer
+ a scoped Agent token + the unauthenticated case) drives the real FastAPI app via
TestClient, and each HTTP outcome must match `can(role, action, scopes)` — a 403
appears **exactly** when the role may not act.

Expectations are derived from `can` (the same oracle the routes' `require_action`
consults), so the gate cannot drift from the source of truth. Hermetic: temp
SQLite, in-process ASGI, no Supabase.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_core.identity.roles import Action, can
from kantaq_core.tracker.service import TrackerService
from kantaq_db.models import Workspace
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock

AGENT_SCOPES = ("tickets.read", "proposals.write")
PRINCIPALS = ("Owner", "Maintainer", "Member", "Viewer", "Agent")


@dataclass(frozen=True)
class Probe:
    label: str
    method: str
    path: str  # may carry {ticket_id}
    action: Action
    body: dict | None = None


# One probe per discriminating surface; `action` is the Action the route enforces.
PROBES: tuple[Probe, ...] = (
    Probe("read-tickets", "GET", "/v1/tickets", Action.tickets_read),
    Probe("read-conflicts", "GET", "/v1/conflicts", Action.tickets_read),
    Probe("read-members", "GET", "/v1/members", Action.members_read),
    Probe("read-telemetry", "GET", "/v1/telemetry", Action.telemetry_read),
    Probe("create-ticket", "POST", "/v1/tickets", Action.tickets_write, {"title": "authz"}),
    Probe(
        "comment",
        "POST",
        "/v1/tickets/{ticket_id}/comments",
        Action.tickets_write,
        {"body": "authz"},
    ),
    Probe("change-telemetry", "PUT", "/v1/telemetry", Action.telemetry_write, {"enabled": True}),
    Probe("invite-member", "POST", "/v1/members/invite", Action.members_invite, None),
)


@dataclass
class Arena:
    client: TestClient
    tokens: dict[str, str]
    project_id: str
    ticket_id: str


@pytest.fixture(scope="module")
def arena(tmp_path_factory: pytest.TempPathFactory) -> Arena:
    """One runtime, one token per role, a seeded project + ticket."""
    db_path = tmp_path_factory.mktemp("roleauthz") / "local.sqlite"
    db: Engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        owner = IdentityService(session).bootstrap_owner()
        assert owner is not None
        tokens = {"Owner": owner.plaintext}
        for role, scopes in (
            (Role.maintainer, []),
            (Role.member, []),
            (Role.viewer, []),
            (Role.agent, list(AGENT_SCOPES)),
        ):
            minted = IdentityService(session).invite(
                email=f"{role.value.lower()}@authz.local", role=role, scopes=scopes
            )
            tokens[role.value] = minted.plaintext
        workspace = Workspace(name="kantaq")
        session.add(workspace)
        session.commit()
        tracker = TrackerService(session, actor_id=owner.member_id, source="app")
        project = tracker.create_project(workspace_id=workspace.id, name="Authz")
        ticket = tracker.create_ticket(project_id=project.id, title="authz ticket")
        project_id, ticket_id = project.id, ticket.id
        session.commit()
    app = create_app(
        settings=Settings(local_db_path=str(db_path)),
        engine=db,
        verifier=TokenVerifier(db, now=FakeClock().monotonic),
    )
    return Arena(TestClient(app), tokens, project_id, ticket_id)


def _expected_allowed(principal: str, action: Action) -> bool:
    if principal == "Agent":
        return can(Role.agent, action, scopes=list(AGENT_SCOPES))
    return can(Role(principal), action)


def _call(arena: Arena, probe: Probe, principal: str) -> int:
    body = probe.body
    if probe.label == "create-ticket":
        body = {**probe.body, "project_id": arena.project_id}  # type: ignore[dict-item]
    elif probe.label == "invite-member":
        body = {"email": f"invitee-{principal}@authz.local", "role": "Member", "scopes": []}
    resp = arena.client.request(
        probe.method,
        probe.path.format(ticket_id=arena.ticket_id),
        json=body,
        headers={"Authorization": f"Bearer {arena.tokens[principal]}"},
    )
    return resp.status_code


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.label)
def test_role_outcome_matches_can(arena: Arena, probe: Probe, principal: str) -> None:
    """A 403 appears exactly when `can()` says the principal may not act."""
    status = _call(arena, probe, principal)
    allowed = _expected_allowed(principal, probe.action)
    if allowed:
        assert status != 403, f"{principal} should be allowed {probe.action.value} but got {status}"
    else:
        assert status == 403, f"{principal} should be denied {probe.action.value} but got {status}"


def test_unauthenticated_request_is_401(arena: Arena) -> None:
    """No bearer token → 401 (bearer required), distinct from a role's 403."""
    resp = arena.client.get("/v1/tickets")
    assert resp.status_code == 401
