"""Lightweight harness-local entities for the v0.0.5 collections.

These mirror the protocol collections (architecture §6) just enough for tests to
build setup data and for ``FakeBackend`` to store events. They are intentionally
*not* the real SQLModel models (MOD-02) so the harness is parallel-safe with E02;
when E02 lands, model-aware builders can produce real rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Op = Literal["patch", "append", "tombstone"]


@dataclass
class PrivacyClass:
    visibility: Literal["local", "team"] = "team"
    hosting_mode: Literal["plain"] = "plain"
    retention_policy: Literal["standard"] = "standard"


@dataclass
class Event:
    event_id: str
    collection: str
    entity_id: str
    actor_id: str
    actor_seq: int
    op: Op = "patch"
    base_rev: int | None = None
    policy_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sig: str | None = None  # Ed25519 signature arrives in v0.1 (E03)


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
    privacy_class: PrivacyClass = field(default_factory=PrivacyClass)


@dataclass
class Comment:
    id: str
    ticket_id: str
    author_id: str
    body: str


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
