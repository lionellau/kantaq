"""Base roles and the permission matrix (PRD §11, FR-E06-7).

Five roles, checked wherever a surface exists. v0.0.5 has two live surfaces
(web/API via the runtime); the other seven (MCP tool calls, memory reads,
memory writes, ticket writes, agent proposals, sync commits, conflict
resolution — NFR-E06-3) wire in as their modules land, each calling ``can``.

``Agent`` is deliberately absent from ``ROLE_PERMISSIONS``: an agent's access
is defined by its token's ``scopes`` (PRD §11 "scoped access defined by
token"), so ``can`` consults the scope list instead of a role row.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """The 5 base roles (PRD §11). Stored on ``members.role``."""

    owner = "Owner"
    maintainer = "Maintainer"
    member = "Member"
    viewer = "Viewer"
    agent = "Agent"


class Action(StrEnum):
    """Permission-checked actions. Grows one surface at a time (NFR-E06-3)."""

    members_read = "members.read"
    members_invite = "members.invite"
    members_revoke = "members.revoke"
    tokens_rotate = "tokens.rotate"
    # The tracker surface (E12 / MOD-03): one read and one write action cover
    # projects, tickets, comments, and attachments. "Ticket writes" is one of
    # the NFR-E06-3 surfaces; finer per-collection actions arrive only if a
    # module needs them.
    tickets_read = "tickets.read"
    tickets_write = "tickets.write"
    # The MCP propose surface (E09/E10, MOD-08/09): storing an agent_proposal
    # for human review. Weaker than tickets.write — a proposal changes nothing
    # until approved — so agent tokens carry it in scopes without ever holding
    # a direct-write action (FR-E09-4 propose-first default).
    proposals_write = "proposals.write"
    # The memory surface (E13 / MOD-19): two of the NFR-E06-3 surfaces. One
    # read and one write action cover entries and links; the per-entry
    # memory policy (MOD-21) layers on top of this coarse check.
    memory_read = "memory.read"
    memory_write = "memory.write"
    # The memory-promotion approval (E13-T4 / MOD-19 §52): approving a proposed
    # team entry into the shared collection is a *human* decision in the Inbox,
    # strictly stronger than memory.write. Agents propose (memory.write) but
    # must never carry this scope, so an agent token gets 403 on approve —
    # mirroring proposals' propose-first default.
    memory_approve = "memory.approve"
    # The telemetry surface (E28, MOD-25): every human may inspect what the
    # machine collects (the privacy promise is transparency); only admins flip
    # the opt-in. Agents get neither unless a token explicitly scopes it.
    telemetry_read = "telemetry.read"
    telemetry_write = "telemetry.write"


# Human roles → allowed actions. Owner is full admin; Maintainer manages
# members and agent tokens; Member and Viewer can see the member list.
# Members do the team's tracker work (read + write); Viewers read it.
ROLE_PERMISSIONS: dict[Role, frozenset[Action]] = {
    Role.owner: frozenset(Action),
    Role.maintainer: frozenset(
        {
            Action.members_read,
            Action.members_invite,
            Action.members_revoke,
            Action.tokens_rotate,
            Action.tickets_read,
            Action.tickets_write,
            Action.memory_read,
            Action.memory_write,
            Action.memory_approve,
            Action.telemetry_read,
            Action.telemetry_write,
        }
    ),
    Role.member: frozenset(
        {
            Action.members_read,
            Action.tickets_read,
            Action.tickets_write,
            Action.memory_read,
            Action.memory_write,
            Action.memory_approve,
            Action.telemetry_read,
        }
    ),
    Role.viewer: frozenset(
        {
            Action.members_read,
            Action.tickets_read,
            Action.memory_read,
            Action.telemetry_read,
        }
    ),
}


def can(role: Role | str, action: Action, *, scopes: list[str] | None = None) -> bool:
    """May this role perform this action?

    Humans are checked against ``ROLE_PERMISSIONS``. Agents are checked against
    their token's ``scopes`` (the action value must be listed). An unknown role
    string fails closed.
    """
    try:
        resolved = Role(role)
    except ValueError:
        return False
    if resolved is Role.agent:
        return action.value in (scopes or [])
    return action in ROLE_PERMISSIONS[resolved]
