"""Role-aware context bundles, per agent role, over the gateway (FR-E16-1..4).

The CI gate behind `scripts/uat_agent_roles.py` (the reporter). The four locked
agent roles each get a DIFFERENT memory-context bundle from the SAME ticket: a
note is linked in every memory space, then each role calls `role_context_preview`
through the real `Gateway.handle_call`, and the spaces it returns must equal that
role's `include_scopes` — asserted against `kantaq_core.memory_policy.policy_for`,
the very filter the gateway runs. Drift-proof and hermetic (the MOD-08/09 fixtures).
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import Role, VerifiedActor
from kantaq_core.memory.service import MemoryService
from kantaq_core.memory_policy import POLICIES, ROLE_SLUGS, policy_for
from kantaq_db.models import Ticket
from kantaq_mcp.catalog import CATALOG
from kantaq_mcp.gateway import Gateway
from kantaq_mcp.session import (
    AUDIT_POLICY_STANDARD,
    COLLECTION_SCOPE_ALL,
    WRITE_MODE_PROPOSE_ONLY,
    GatewaySession,
)
from kantaq_test_harness.clock import FakeClock

# The seven memory spaces; include ∪ exclude partitions these for every role.
SPACES = ("codebase", "decision", "ticket", "project", "release", "workspace", "agent_run")
GRANTED = ("tickets.read", "memory.read", "proposals.write")


@pytest.fixture
def context_board(engine: Engine, owner, clock: FakeClock, ticket: Ticket) -> dict[str, str]:  # noqa: ANN001
    """Link one team note in every memory space to the seeded ticket.

    Returns {entry_id: space} so a bundle's included ids map back to spaces.
    """
    mem_by_space: dict[str, str] = {}
    with Session(engine) as session:
        mem = MemoryService(session, actor_id=owner.member_id, source="app", now=clock.now)
        for space in SPACES:
            entry = mem.create_entry(
                title=f"{space} note", body=f"a {space} note", space=space, visibility="team"
            )
            mem.link(entry.id, ticket.id, reason="context-board")
            mem_by_space[entry.id] = space
    return mem_by_space


def _session_for(agent, clock: FakeClock, role: str) -> GatewaySession:  # noqa: ANN001
    now = clock.now().replace(tzinfo=None)
    allowed = tuple(spec.name for spec in CATALOG if spec.required_action in set(GRANTED))
    return GatewaySession(
        session_id=f"s-ctx-{role}",
        member_id=agent.member_id,
        role=Role.agent.value,
        token_id="tok-ctx",
        scopes=GRANTED,
        allowed_tools=allowed,
        write_mode=WRITE_MODE_PROPOSE_ONLY,
        created_at=now,
        expires_at=now.replace(year=2030),
        collection_scope=(COLLECTION_SCOPE_ALL,),
        granted_verbs=GRANTED,
        agent_role=role,
        memory_policy_id=None,
        audit_policy=AUDIT_POLICY_STANDARD,
        grant_id=None,
    )


def _bundle_spaces(
    gateway: Gateway,
    actor: VerifiedActor,
    session: GatewaySession,
    ticket_id: str,
    mem_by_space: dict[str, str],
) -> set[str]:
    result = gateway.handle_call(
        actor=actor,
        session=session,
        tool_name="role_context_preview",
        args={"ticket_id": ticket_id},
    )
    included = {e["id"] for e in result["bundle"]["included"]}
    return {mem_by_space[i] for i in included if i in mem_by_space}


@pytest.mark.parametrize("role", ROLE_SLUGS)
def test_role_bundle_equals_its_include_scopes(
    role: str,
    gateway: Gateway,
    agent,  # noqa: ANN001
    clock: FakeClock,
    ticket: Ticket,
    context_board: dict[str, str],
) -> None:
    """Each agent role's bundle is exactly the spaces in its policy include_scopes."""
    actor = gateway.authenticate(agent.plaintext)
    assert actor is not None
    got = _bundle_spaces(gateway, actor, _session_for(agent, clock, role), ticket.id, context_board)
    want = set(policy_for(role).include_scopes) & set(SPACES)
    assert got == want, f"{role}: bundle {sorted(got)} != include_scopes {sorted(want)}"


def test_roles_get_distinct_bundles(
    gateway: Gateway,
    agent,  # noqa: ANN001
    clock: FakeClock,
    ticket: Ticket,
    context_board: dict[str, str],
) -> None:
    """The whole point of role-awareness: the four roles do not all see the same
    context. At least two roles must differ on at least one space."""
    actor = gateway.authenticate(agent.plaintext)
    assert actor is not None
    bundles = {
        role: frozenset(
            _bundle_spaces(
                gateway, actor, _session_for(agent, clock, role), ticket.id, context_board
            )
        )
        for role in ROLE_SLUGS
    }
    assert len(set(bundles.values())) > 1, "every role saw an identical bundle — not role-aware"
    # Concrete contrast pinned: codebase is for code/qa, not design/product.
    assert "codebase" in bundles["code_agent"]
    assert "codebase" not in bundles["design_agent"]
    # agent_run scratch is private to no role.
    assert all("agent_run" not in spaces for spaces in bundles.values())


def test_manifest_has_four_roles() -> None:
    """Guard against a policy being added/removed without updating this gate."""
    assert len(POLICIES) == 4
    assert set(ROLE_SLUGS) == {"code_agent", "qa_agent", "design_agent", "product_agent"}
