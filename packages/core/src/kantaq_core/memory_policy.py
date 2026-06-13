"""Memory-read policy — the per-role filter on what an agent may read (MOD-21 / E16).

A memory policy is the fine, per-session layer the MCP gateway consults on every
memory read (the 8-check list, check 6 — PRD §8.5/§6.11). It decides which memory
entries a role-scoped agent session may see, by three independent gates:

1. **privacy filter** — the entry's privacy class must clear the policy floor.
   In v0.1 that floor is ``visibility == "team"``: a ``local`` entry never clears
   it, so the resolver can never hand an agent another actor's private note
   (**NFR-E16-1**, true *by construction* — the privacy gate is checked first and
   is decisive, not a downstream filter that could be bypassed).
2. **review-status filter** — ``stale``/``rejected`` memory is withheld; only
   reviewed, current context feeds an agent.
3. **scope** — the entry's memory *space* (FR-E13-4: ``codebase``/``decision``/…)
   must be in the role's ``include_scopes`` and not in its ``exclude_scopes``.

Policies are **data, not code** (§6.11) but **hardcoded in v0.1** (§8.9): like the
lifecycle taxonomy (MOD-20), each policy is a frozen record and the registry is a
tuple — no I/O, no session, no migration. User-editable, db-backed policies are
v0.2 (the §12 ``memory_policies`` table name is reserved for then); per-ticket
overrides are DEBT-13 (v0.3). Keeping policy in a pure module now means the
gateway wires ``filter`` into a session in Sprint 4 without a schema touch.

This module is strict: an unknown role raises ``UnknownAgentRoleError``. ``filter``
is total over the four locked agent roles (FR-E16-1); the human teammate is a
graded *baseline* in the eval set, not a resolver role, so it has no policy here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

# Visibility ranks low → high; the privacy floor admits everything at or above it.
# Memory rows are ``local`` or ``team`` in v0.1 (``public`` is reserved); an agent
# floor of ``team`` therefore rejects exactly the ``local`` rows.
_VISIBILITY_RANK: Final[dict[str, int]] = {"local": 0, "team": 1, "public": 2}


class AgentRole(StrEnum):
    """The four locked agent context roles the resolver serves (FR-E16-1, §8.9).

    Distinct from the base roles (Owner/Member/… in :mod:`kantaq_core.identity.roles`):
    a base role says *who you are*, an agent role says *what an agent is doing on a
    ticket*, and it is the agent role that selects a memory policy.
    """

    code_agent = "code_agent"
    qa_agent = "qa_agent"
    design_agent = "design_agent"
    product_agent = "product_agent"


# Review statuses withheld from every agent: stale/rejected context is worse than
# none. ``draft``/``proposed``/``approved`` pass (the promotion workflow is v0.2).
_WITHHELD_REVIEW_STATUSES: Final[frozenset[str]] = frozenset({"stale", "rejected"})


class UnknownAgentRoleError(ValueError):
    """The role is not one of the four locked agent roles (FR-E16-1)."""

    def __init__(self, role: str) -> None:
        super().__init__(f"unknown agent role {role!r}; expected one of {ROLE_SLUGS}")
        self.role = role


@dataclass(frozen=True)
class PrivacyFilter:
    """The minimum privacy class an entry must satisfy to be included (§6.11).

    v0.1 floors on ``visibility`` only (memory rows carry no per-entry
    ``hosting_mode``/``retention_policy`` — those bind at the collection level,
    §6.10); the extra dimensions get fields here when a collection needs them.
    """

    min_visibility: str = "team"

    def admits(self, visibility: str) -> bool:
        floor = _VISIBILITY_RANK[self.min_visibility]
        return _VISIBILITY_RANK.get(visibility, -1) >= floor


@dataclass(frozen=True)
class MemoryPolicy:
    """One role's read policy (§6.11): which memory spaces, at what privacy floor."""

    policy_id: str
    applies_to_role: AgentRole
    include_scopes: tuple[str, ...]
    exclude_scopes: tuple[str, ...]
    privacy_filter: PrivacyFilter
    rationale: str
    withheld_review_statuses: frozenset[str] = _WITHHELD_REVIEW_STATUSES


class MemoryReadable(Protocol):
    """The fields :func:`filter` reads — satisfied by ``MemoryEntry`` and the
    eval-fixture rows, so the policy is testable without a database session.

    Declared **read-only** (properties, not settable attributes): the policy only
    reads these fields, and a read-only protocol is satisfied by both the mutable
    ORM ``MemoryEntry`` and the frozen ``EvalMemory`` fixture row (a frozen
    dataclass cannot satisfy a settable-attribute protocol)."""

    @property
    def id(self) -> str: ...
    @property
    def space(self) -> str: ...
    @property
    def visibility(self) -> str: ...
    @property
    def review_status(self) -> str: ...
    @property
    def expires_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class PolicyDecision:
    """Why one entry was kept or dropped — the structured reason the preview shows."""

    entry_id: str
    included: bool
    reason: str


@dataclass(frozen=True)
class PolicyFilterResult:
    """The partition :func:`filter` returns: kept entries and dropped ones (reasoned)."""

    included: tuple[MemoryReadable, ...]
    excluded: tuple[tuple[MemoryReadable, str], ...]
    decisions: tuple[PolicyDecision, ...]


# The four locked policies. Include ∪ exclude partitions all seven memory spaces
# for every role (pinned in tests), so a new space cannot land without a
# deliberate per-role decision — and no entry is silently "neither". The split is
# what gives the eval its signal: a ``codebase`` note is in for code/qa and out for
# design/product; a ``release`` note is in for qa/product and out for code/design.
POLICIES: Final[tuple[MemoryPolicy, ...]] = (
    MemoryPolicy(
        policy_id="memory-policy/code_agent/v1",
        applies_to_role=AgentRole.code_agent,
        include_scopes=("codebase", "decision", "ticket", "project"),
        exclude_scopes=("release", "workspace", "agent_run"),
        privacy_filter=PrivacyFilter(min_visibility="team"),
        rationale=(
            "Implementation reads the code's architecture, the decisions that "
            "constrain it, the ticket's own context, and the project goal — not "
            "release comms, broad workspace notes, or another agent's run scratch."
        ),
    ),
    MemoryPolicy(
        policy_id="memory-policy/qa_agent/v1",
        applies_to_role=AgentRole.qa_agent,
        include_scopes=("ticket", "release", "codebase", "decision"),
        exclude_scopes=("workspace", "project", "agent_run"),
        privacy_filter=PrivacyFilter(min_visibility="team"),
        rationale=(
            "QA verifies behavior and regressions: it needs the ticket's "
            "acceptance notes, the release/rollback plan, the code under test, and "
            "the decisions that define expected behavior — not high-level framing."
        ),
    ),
    MemoryPolicy(
        policy_id="memory-policy/design_agent/v1",
        applies_to_role=AgentRole.design_agent,
        include_scopes=("project", "ticket", "decision", "workspace"),
        exclude_scopes=("codebase", "release", "agent_run"),
        privacy_filter=PrivacyFilter(min_visibility="team"),
        rationale=(
            "Design shapes UX from the product/project context, the ticket's "
            "requirements, workspace conventions, and prior design decisions — not "
            "codebase internals or release operations."
        ),
    ),
    MemoryPolicy(
        policy_id="memory-policy/product_agent/v1",
        applies_to_role=AgentRole.product_agent,
        include_scopes=("workspace", "project", "ticket", "decision", "release"),
        exclude_scopes=("codebase", "agent_run"),
        privacy_filter=PrivacyFilter(min_visibility="team"),
        rationale=(
            "Product framing needs the widest business context — workspace norms, "
            "project goals, the ticket, decisions, and release/outcome notes — but "
            "not codebase internals or an agent's private run scratch."
        ),
    ),
)

ROLE_SLUGS: Final[tuple[str, ...]] = tuple(policy.applies_to_role.value for policy in POLICIES)

_BY_ROLE: Final[dict[AgentRole, MemoryPolicy]] = {p.applies_to_role: p for p in POLICIES}


def policies() -> tuple[MemoryPolicy, ...]:
    """Every locked policy, one per agent role (MOD-21 interface)."""
    return POLICIES


def is_agent_role(role: str) -> bool:
    """Whether the slug is one of the four locked agent roles."""
    return role in ROLE_SLUGS


def policy_for(role: AgentRole | str) -> MemoryPolicy:
    """The policy for an agent role. Unknown roles fail closed (raise)."""
    try:
        resolved = AgentRole(role)
    except ValueError:
        raise UnknownAgentRoleError(str(role)) from None
    return _BY_ROLE[resolved]


def decide(policy: MemoryPolicy, entry: MemoryReadable, *, now: datetime) -> PolicyDecision:
    """Decide one entry against one policy, returning the first failing gate.

    Gate order is deliberate and security-load-bearing: **privacy first**, so a
    ``local`` entry is dropped for privacy (``privacy_filter:visibility_local``)
    regardless of its space — NFR-E16-1 cannot be defeated by a scope rule. Then
    expiry, then review status, then the scope lists. An entry that clears every
    gate is included with the scope that admitted it.
    """
    if not policy.privacy_filter.admits(entry.visibility):
        return PolicyDecision(entry.id, False, f"privacy_filter:visibility_{entry.visibility}")
    if entry.expires_at is not None and entry.expires_at <= now:
        return PolicyDecision(entry.id, False, "expired")
    if entry.review_status in policy.withheld_review_statuses:
        return PolicyDecision(entry.id, False, f"review_status:{entry.review_status}")
    if entry.space in policy.exclude_scopes:
        return PolicyDecision(entry.id, False, f"exclude_scope:{entry.space}")
    if entry.space not in policy.include_scopes:
        return PolicyDecision(entry.id, False, f"out_of_scope:{entry.space}")
    return PolicyDecision(entry.id, True, f"include_scope:{entry.space}")


def filter(  # noqa: A001 — the domain verb; this is the MOD-21 interface name
    entries: Sequence[MemoryReadable],
    policy: MemoryPolicy,
    *,
    now: datetime,
) -> PolicyFilterResult:
    """Partition entries into included / excluded(reason) under a policy.

    The enforcement helper the gateway calls in Sprint 4 (and the eval set grades
    against). Pure: ``now`` is injected so expiry is deterministic under FakeClock.
    No ``local`` entry is ever in ``included`` — the privacy gate guarantees it
    (NFR-E16-1), and the result is reasoned end to end for the preview surface.
    """
    included: list[MemoryReadable] = []
    excluded: list[tuple[MemoryReadable, str]] = []
    decisions: list[PolicyDecision] = []
    for entry in entries:
        decision = decide(policy, entry, now=now)
        decisions.append(decision)
        if decision.included:
            included.append(entry)
        else:
            excluded.append((entry, decision.reason))
    return PolicyFilterResult(
        included=tuple(included),
        excluded=tuple(excluded),
        decisions=tuple(decisions),
    )


__all__ = [
    "AgentRole",
    "MemoryPolicy",
    "MemoryReadable",
    "PolicyDecision",
    "PolicyFilterResult",
    "PrivacyFilter",
    "POLICIES",
    "ROLE_SLUGS",
    "UnknownAgentRoleError",
    "decide",
    "filter",
    "is_agent_role",
    "policies",
    "policy_for",
]
