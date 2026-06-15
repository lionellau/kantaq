"""The role matrix fails closed and matches PRD §11 (FR-E06-7)."""

from __future__ import annotations

from kantaq_core.identity import Action, Role, can


def test_owner_can_do_everything() -> None:
    assert all(can(Role.owner, action) for action in Action)


def test_maintainer_manages_members_and_tokens() -> None:
    assert can(Role.maintainer, Action.members_read)
    assert can(Role.maintainer, Action.members_invite)
    assert can(Role.maintainer, Action.members_revoke)
    assert can(Role.maintainer, Action.tokens_rotate)


def test_member_and_viewer_can_only_read_members() -> None:
    for role in (Role.member, Role.viewer):
        assert can(role, Action.members_read)
        assert not can(role, Action.members_invite)
        assert not can(role, Action.members_revoke)
        assert not can(role, Action.tokens_rotate)


def test_agent_is_scoped_by_token_not_role() -> None:
    # PRD §11: Agent access is whatever the token's scopes say — nothing more.
    assert not can(Role.agent, Action.members_read)
    assert can(Role.agent, Action.members_read, scopes=["members.read"])
    assert not can(Role.agent, Action.members_invite, scopes=["members.read"])


def test_memory_approve_is_human_only_propose_first() -> None:
    """E13-T4: humans (Maintainer, Member) approve; Viewer cannot; an agent
    holding only memory.write/read can propose but never approve."""
    assert can(Role.maintainer, Action.memory_approve)
    assert can(Role.member, Action.memory_approve)
    assert not can(Role.viewer, Action.memory_approve)
    # The propose-first guard: a write-scoped agent token lacks memory.approve.
    agent_scopes = ["memory.read", "memory.write"]
    assert can(Role.agent, Action.memory_write, scopes=agent_scopes)
    assert not can(Role.agent, Action.memory_approve, scopes=agent_scopes)


def test_unknown_role_fails_closed() -> None:
    assert not can("Superuser", Action.members_read)
    assert not can("", Action.members_read)


def test_role_values_are_the_stored_strings() -> None:
    # members.role stores these exact strings (PRD §11 capitalization).
    assert [r.value for r in Role] == ["Owner", "Maintainer", "Member", "Viewer", "Agent"]
