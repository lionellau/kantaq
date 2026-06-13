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

from sqlalchemy import JSON, Column, UniqueConstraint
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
    # Attachment refs (FR-E12-4, D-13): a list of blob-ref dicts
    # {blob_id, filename, media_type, size_bytes}; the bytes live in the blob
    # store (local filesystem in solo mode), never in the row. Attachment
    # content is untrusted (PRD §15) — stored, never opened or executed.
    attachments: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )


class Comment(CollectionBase, table=True):
    __tablename__ = "comments"

    ticket_id: str = Field(foreign_key="tickets.id", index=True)
    author_actor_id: str
    body: str


class TicketRelationship(CollectionBase, table=True):
    """A typed edge between two tickets (MOD-03 v0.1 / FR-E12-3).

    The five relation types (``related``/``blocked-by``/``blocking``/
    ``duplicate``/``caused-by``) live in ``kantaq_core.tracker`` — the one write
    path validates the vocabulary and the integrity rules (no self-link, no
    duplicate including the symmetric/inverse spelling, no dependency cycle).
    Both endpoints are tickets in the same workspace; the row carries no mutable
    fields (an edge is created and tombstoned, never patched), so it syncs
    ``lww`` like any collection. The ``UNIQUE`` backs the duplicate rule at the
    database for the exact spelling; the symmetric/inverse collapse is the
    service's job (no portable SQL expresses it across both dialects).
    """

    __tablename__ = "ticket_relationships"
    __table_args__ = (UniqueConstraint("from_id", "to_id", "type", name="uq_ticket_relationship"),)

    from_id: str = Field(foreign_key="tickets.id", index=True)
    to_id: str = Field(foreign_key="tickets.id", index=True)
    # type ∈ related | blocked-by | blocking | duplicate | caused-by (VARCHAR
    # for dialect parity; the vocabulary is enforced in the service).
    type: str = Field(max_length=16)
    created_by: str | None = Field(default=None)


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


class MemoryEntry(CollectionBase, table=True):
    """Scoped context separate from work state (MOD-19 / FR-E13-1).

    The first real user of the privacy class: rows with ``visibility="local"``
    are ``private_local`` — they never produce a sync event (NFR-E13-1, enforced
    in ``kantaq_core.memory``). Field vocabularies (type/source/space/confidence/
    review_status) are validated in the service, stored as portable VARCHARs.
    """

    __tablename__ = "memory_entries"

    title: str
    body: str = ""
    # type ∈ note | decision | constraint | learning | reference
    type: str = Field(default="note", max_length=16)
    # source ∈ manual | agent | import — how the entry entered the system.
    source: str = Field(default="manual", max_length=16)
    # space ∈ workspace | project | ticket | codebase | decision | release |
    # agent_run (FR-E13-4): a grouping field, not a table.
    space: str = Field(default="workspace", max_length=16)
    # Loose "collection/id" refs; the typed ticket links live in memory_links.
    linked_entities: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # {origin, actor_id, captured_at, detail?} — who/when/how (PRD §15).
    provenance: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # confidence ∈ low | medium | high (categorical, PRD §8.10 reasoning).
    confidence: str = Field(default="medium", max_length=8)
    # review_status ∈ draft | proposed | approved | stale | rejected; v0.1
    # writes allow draft/stale only (promotion is the v0.2 human-gated flow).
    review_status: str = Field(default="draft", max_length=16)
    expires_at: datetime | None = Field(default=None)
    created_by: str | None = Field(default=None)


class MemoryLink(CollectionBase, table=True):
    """A manual ticket↔memory link with a reason (MOD-19 / FR-E13-2).

    A link inherits the stricter visibility of its endpoints: a link to a
    ``local`` entry is itself ``local`` (the service sets it and never emits an
    event for it), so the existence of a private note cannot leak via its link.
    """

    __tablename__ = "memory_links"
    __table_args__ = (UniqueConstraint("ticket_id", "memory_id", name="uq_memory_link_pair"),)

    ticket_id: str = Field(foreign_key="tickets.id", index=True)
    memory_id: str = Field(foreign_key="memory_entries.id", index=True)
    reason: str
    created_by: str | None = Field(default=None)


class Device(CollectionBase, table=True):
    """A runtime's registered signing identity (MOD-06 v0.1, D-01).

    One row per local runtime: the Ed25519 *verify* key only — the private
    seed lives in that machine's keychain and never enters any table. The
    set of active device rows is the root-of-trust map grant verification
    resolves issuers against (MOD-17 ``verify_grant`` roots).
    """

    __tablename__ = "devices"

    # 64 lowercase hex chars (32-byte Ed25519 verify key); one row per key.
    public_key: str = Field(unique=True, max_length=64)
    member_id: str | None = Field(default=None, foreign_key="members.id", index=True)
    label: str = ""
    # Set when the device is decommissioned; a revoked device is no longer a
    # verification root and can issue nothing.
    revoked_at: datetime | None = Field(default=None)


class CapabilityGrantRow(CollectionBase, table=True):
    """A stored capability grant (MOD-06 v0.1, PRD §6.9).

    The signed fields mirror ``kantaq_protocol.CapabilityGrant`` exactly —
    ``issued_at``/``expires_at`` are unix seconds (ints), not datetimes, so
    the row reconstructs byte-identical signing bytes. ``token_id`` links the
    grant to the member token that authorized issuance: rotating or revoking
    that token revokes its derived grants (FR-E06-6's v0.1 slice).
    Merge policy is ``authoritative_tx`` — never optimistically synced.
    """

    __tablename__ = "capability_grants"

    subject: str = Field(foreign_key="members.id", index=True)
    issuer: str = Field(foreign_key="devices.id", index=True)
    resource: str
    verbs: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    issued_at: int
    expires_at: int
    revokes: str | None = Field(default=None)
    sig: str | None = Field(default=None, max_length=128)
    token_id: str | None = Field(default=None, foreign_key="tokens.id", index=True)
    revoked_at: datetime | None = Field(default=None)


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


class EventLog(SQLModel, table=True):
    """The local append-only event log (MOD-04 / FR-E04-1).

    Local infrastructure like ``schema_version`` (no privacy envelope): the
    rows *inside* it are the protocol events. ``committed_rev`` is NULL until
    the backend assigns a commit order (push ack or pull); the table state of
    a collection is the fold of its events ordered by commit order, local
    pending last. Dedup is by ``(actor_id, actor_seq)`` (NFR-E04-2) — the
    unique constraint makes a duplicate insert impossible, not just unlikely.
    """

    __tablename__ = "event_log"
    __table_args__ = (UniqueConstraint("actor_id", "actor_seq", name="uq_event_actor_seq"),)

    event_id: str = Field(primary_key=True, max_length=26)
    collection: str = Field(index=True, max_length=32)
    entity_id: str = Field(index=True, max_length=26)
    actor_id: str = Field(max_length=26)
    actor_seq: int
    op: str = Field(max_length=16)  # patch | append | tombstone
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    base_rev: int | None = Field(default=None)
    policy_ref: str | None = Field(default=None)
    sig: str | None = Field(default=None)  # Ed25519 arrives in v0.1 (MOD-17)
    committed_rev: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class SyncCursor(SQLModel, table=True):
    """Per-collection pull cursors (MOD-04 / FR-E04-2).

    One row per (collection, actor): the highest backend revision this replica
    has ingested and acked for that collection ("*" = the all-collections
    stream). Local infrastructure, never synced.
    """

    __tablename__ = "sync_cursors"

    collection: str = Field(primary_key=True, max_length=32)
    actor_id: str = Field(primary_key=True, max_length=26)
    acked_rev: int = Field(default=0)
    updated_at: datetime = Field(default_factory=_utcnow)


class TelemetryEvent(SQLModel, table=True):
    """Opt-in local telemetry (MOD-25 / FR-E28-1..2, D-10).

    Local infrastructure like ``schema_version``: deliberately **not** a
    syncable collection (absent from ``COLLECTION_META``/``COLLECTION_MODELS``),
    so no sync path can ever pick a row up — telemetry never leaves the machine.
    ``props`` holds only numeric/categorical values vetted by the
    ``kantaq_core.telemetry`` registry; ticket/memory content is rejected at
    record time, not just by convention.
    """

    __tablename__ = "telemetry_events"

    id: str = Field(default_factory=lambda: new_id(), primary_key=True, max_length=26)
    name: str = Field(index=True, max_length=64)
    props: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=_utcnow)


class LocalSetting(SQLModel, table=True):
    """Per-machine key/value settings (first user: the telemetry opt-in flag).

    Local infrastructure, never synced — a machine-scoped preference must not
    follow a workspace to other replicas (D-10: telemetry is per-install).
    """

    __tablename__ = "local_settings"

    key: str = Field(primary_key=True, max_length=64)
    value: str = Field(max_length=256)
    updated_at: datetime = Field(default_factory=_utcnow)


def new_id() -> str:
    """ULID factory indirection so tests can read the id scheme in one place."""
    from kantaq_db.ids import new_ulid

    return new_ulid()


# The 13 collection table classes, in the canonical order (matches meta.py).
COLLECTION_MODELS: tuple[type[CollectionBase], ...] = (
    Workspace,
    Project,
    Ticket,
    Comment,
    TicketRelationship,
    Member,
    Token,
    AuditEvent,
    AgentProposal,
    MemoryEntry,
    MemoryLink,
    Device,
    CapabilityGrantRow,
)
