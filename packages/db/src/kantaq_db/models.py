"""SQLModel models for the v0.0.5 collections (FR-E02-1).

One definition, two stores: these models compile to both SQLite (local replica)
and Postgres (Supabase backend) from the same metadata (D-07). To keep the two
dialects in lock-step we deliberately use only portable column types — plain
``str`` (VARCHAR) for enum-like fields instead of a native Postgres ``ENUM``,
and SQLAlchemy's generic ``JSON`` for list/dict fields (JSON in SQLite, JSON in
Postgres). See ``parity.py`` for the check that proves it.

Every collection row carries the same envelope (``CollectionBase``): a ULID
``id``, ``created_at`` / ``updated_at``, an ``actor_seq`` for per-actor ordering
(kept from day one per D-05), and the three ``privacy_class`` columns (D-14).
Field detail for each collection lives in its owning domain spec — MOD-03
(tracker), MOD-06 (identity), MOD-07 (audit).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CollectionBase(SQLModel):
    """Shared envelope for every syncable collection row.

    A non-table base: each ``table=True`` subclass inherits these as real
    columns. ``privacy_class`` is stored as three flat columns so it indexes and
    filters cleanly in both dialects.
    """

    id: str = Field(default_factory=lambda: new_id(), primary_key=True, max_length=26)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    actor_seq: int = Field(default=0)
    # privacy_class (D-14): visibility ∈ {local, team}, hosting_mode = plain,
    # retention_policy = standard in MVP; other values reserved (DEBT-11).
    visibility: str = Field(default="team", max_length=16)
    hosting_mode: str = Field(default="plain", max_length=16)
    retention_policy: str = Field(default="standard", max_length=16)


class Workspace(CollectionBase, table=True):
    __tablename__ = "workspaces"

    name: str


class Project(CollectionBase, table=True):
    __tablename__ = "projects"

    workspace_id: str = Field(foreign_key="workspaces.id", index=True)
    name: str
    goal: str = ""
    scope: str = ""
    owner: str | None = Field(default=None)
    target_date: datetime | None = Field(default=None)
    status: str = Field(default="active", max_length=32)


class Ticket(CollectionBase, table=True):
    __tablename__ = "tickets"

    project_id: str = Field(foreign_key="projects.id", index=True)
    title: str
    description: str = ""
    status: str = Field(default="todo", max_length=32)
    priority: str = Field(default="medium", max_length=16)
    labels: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    assignee: str | None = Field(default=None)
    due_date: datetime | None = Field(default=None)
    acceptance_criteria: str = ""
    lifecycle_stage: str = Field(default="intake", max_length=32)
    parent_id: str | None = Field(default=None, foreign_key="tickets.id", index=True)
    created_by: str | None = Field(default=None)


class Comment(CollectionBase, table=True):
    __tablename__ = "comments"

    ticket_id: str = Field(foreign_key="tickets.id", index=True)
    author_actor_id: str
    body: str


class Member(CollectionBase, table=True):
    __tablename__ = "members"

    workspace_id: str = Field(foreign_key="workspaces.id", index=True)
    email: str = Field(index=True)
    # role ∈ Owner | Maintainer | Member | Viewer | Agent (PRD §11 base roles).
    role: str = Field(default="Member", max_length=16)
    # status ∈ active | invited | revoked (E06). Invited members flip to active
    # on their first authenticated request; revoked members never authenticate.
    status: str = Field(default="active", max_length=16)


class Token(CollectionBase, table=True):
    __tablename__ = "tokens"

    member_id: str = Field(foreign_key="members.id", index=True)
    hashed: str  # never the plaintext token (argon2id PHC string, E06)
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # Set on revoke/rotate. A row with revoked_at never authenticates again;
    # rows are kept (not deleted) so the audit trail can reference them.
    revoked_at: datetime | None = Field(default=None)


class AuditEvent(CollectionBase, table=True):
    __tablename__ = "audit_events"

    actor_id: str = Field(index=True)
    action: str = Field(max_length=64)
    object_ref: str | None = Field(default=None, index=True)
    before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    source: str = Field(default="app", max_length=16)
    chain_hash: str | None = Field(default=None)  # hash chain arrives in v0.1 (E07)


class AgentProposal(CollectionBase, table=True):
    __tablename__ = "agent_proposals"

    ticket_id: str = Field(foreign_key="tickets.id", index=True)
    proposer_id: str
    diff: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="pending", max_length=16)


class SchemaVersion(SQLModel, table=True):
    """Single-row table guarding boot (FR-E02-4).

    Not a syncable collection — it is local infrastructure, so it does not carry
    the privacy envelope. The current schema version and the Alembic revision
    that set it are written by the migration; the runtime compares this against
    the version the code expects and refuses to start on a mismatch.
    """

    __tablename__ = "schema_version"

    version: int = Field(primary_key=True)
    revision: str
    applied_at: datetime = Field(default_factory=_utcnow)


def new_id() -> str:
    """ULID factory indirection so tests can read the id scheme in one place."""
    from kantaq_db.ids import new_ulid

    return new_ulid()


# The 8 collection table classes, in the canonical order (matches meta.py).
COLLECTION_MODELS: tuple[type[CollectionBase], ...] = (
    Workspace,
    Project,
    Ticket,
    Comment,
    Member,
    Token,
    AuditEvent,
    AgentProposal,
)
