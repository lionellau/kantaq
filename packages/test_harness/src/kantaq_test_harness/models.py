"""Lightweight harness-local entities for the v0.0.5 collections.

These mirror the protocol collections (architecture §6) just enough for tests to
build setup data and for ``FakeBackend`` to store events. They are intentionally
*not* the real SQLModel models (MOD-02) so the harness is parallel-safe with E02;
when E02 lands, model-aware builders can produce real rows.

``Event`` stopped being a look-alike when MOD-04 landed: the sync engine owns
the canonical protocol event, and the harness re-exports it so ``FakeBackend``
and the real engine speak the same nominal type (one Event, one truth).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from kantaq_sync_engine.events import Event, Op

__all__ = [
    "AgentProposal",
    "AuditEvent",
    "CapabilityGrantRow",
    "Comment",
    "Device",
    "Event",
    "Member",
    "MemoryEntry",
    "MemoryLink",
    "Milestone",
    "Op",
    "PrivacyClass",
    "Project",
    "SkillContainer",
    "SkillMapping",
    "Ticket",
    "TicketMilestone",
    "TicketRelationship",
    "Token",
    "Workspace",
]


@dataclass
class PrivacyClass:
    visibility: Literal["local", "team"] = "team"
    hosting_mode: Literal["plain"] = "plain"
    retention_policy: Literal["standard"] = "standard"


@dataclass
class Workspace:
    id: str
    name: str
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class Project:
    id: str
    workspace_id: str
    name: str
    goal: str = ""
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class Ticket:
    id: str
    project_id: str
    title: str
    status: str = "todo"
    priority: str = "medium"
    assignee: str | None = None
    # MOD-20 (E14): the taxonomy's entry stage; override per test.
    lifecycle_stage: str = "intake"
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class Comment:
    id: str
    ticket_id: str
    author_id: str
    body: str


@dataclass
class TicketRelationship:
    """Look-alike of the v0.1 typed ticket edge (E12-T3 / MOD-03)."""

    id: str
    from_id: str
    to_id: str
    type: str = "related"


@dataclass
class Milestone:
    """Look-alike of the v0.3 milestone (E14-T2 / MOD-20)."""

    id: str
    project_id: str
    name: str
    description: str = ""
    target_date: datetime | None = None
    status: str = "active"
    created_by: str | None = None


@dataclass
class TicketMilestone:
    """Look-alike of the v0.3 ticket↔milestone membership (E14-T2 / MOD-20)."""

    id: str
    ticket_id: str
    milestone_id: str
    created_by: str | None = None


@dataclass
class Member:
    id: str
    workspace_id: str
    email: str
    role: str = "Member"


@dataclass
class Token:
    id: str
    member_id: str
    hashed: str
    scopes: list[str] = field(default_factory=list)


@dataclass
class AuditEvent:
    id: str
    actor_id: str
    action: str
    target: str
    at: datetime | None = None


@dataclass
class AgentProposal:
    id: str
    ticket_id: str
    proposer_id: str
    diff: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"


@dataclass
class MemoryEntry:
    """Look-alike of the v0.1 memory collection (E13 / MOD-19)."""

    id: str
    title: str
    body: str = ""
    type: str = "note"
    source: str = "manual"
    space: str = "workspace"
    confidence: str = "medium"
    review_status: str = "draft"
    provenance: dict[str, Any] = field(default_factory=dict)
    linked_entities: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class MemoryLink:
    """Ticket↔memory link with a reason (E13 / MOD-19)."""

    id: str
    ticket_id: str
    memory_id: str
    reason: str = "context for this ticket"


@dataclass
class Device:
    """Look-alike of the v0.1 device registration (E06 / MOD-06, D-01)."""

    id: str
    public_key: str
    member_id: str | None = None
    label: str = ""
    revoked_at: datetime | None = None
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class SkillContainer:
    """Look-alike of the v0.2 db-backed skill container (E17-T4 / MOD-22).

    Mirrors the ORM ``SkillContainerRow`` (``recommended_roles`` plural); the
    harness stays a leaf, so no ORM import.
    """

    id: str
    slug: str
    name: str
    recommended_roles: list[str] = field(default_factory=list)
    supported_stages: list[str] = field(default_factory=list)
    required_input: str = ""
    expected_output: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    default_write_mode: str = "read"
    risk_level: str = "low"
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class SkillMapping:
    """Look-alike of the v0.2 db-backed skill→tool mapping (E17-T4 / MOD-22).

    ``connection`` is DEBT-06 descriptive (a label, not an executable command);
    no secret field exists (DEBT-07 moot).
    """

    id: str
    container_id: str
    scope: str = "personal"
    provider: str = ""
    connection: str = ""
    status: str = "active"
    created_by: str | None = None
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class CapabilityGrantRow:
    """Look-alike of the stored v0.1 capability grant (E06 / MOD-06)."""

    id: str
    subject: str
    issuer: str
    resource: str
    verbs: list[str] = field(default_factory=list)
    issued_at: int = 0
    expires_at: int = 0
    revokes: str | None = None
    sig: str | None = None
    token_id: str | None = None
    revoked_at: datetime | None = None
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)
