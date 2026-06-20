"""The self-hosted backend's Postgres schema (MOD-28 / E25-T1).

The self-hosted sync-server stores committed state in **two layers**, both on
the one Postgres instance:

1. **The trust + collection-mirror tables** — generated from the single
   ``SQLModel.metadata`` (``kantaq_db.models``, D-07): ``workspaces``,
   ``members``, ``devices``, ``capability_grants`` and the other collection
   mirrors. The verifier reads ``devices`` + ``capability_grants`` through the
   SHARED ``verification_roots`` / ``local_grant_index`` readers — the exact
   functions the local runtime uses (``src/kantaq/cli.py``), so the self-hosted
   server verifies a grant the same way a replica does. No second trust model.

2. **The ``sync_events`` append-only log** — hand-written here, mirroring
   ``supabase/migrations/0002_sync_events.sql`` field-for-field (D-07 keeps the
   8 collection mirrors generated and the log hand-shaped, the same split the
   Supabase backend uses). ``revision`` is a ``BIGINT GENERATED ALWAYS AS
   IDENTITY`` so commit order is assigned by the database, never by a client
   clock (D-05); ``UNIQUE (actor_id, actor_seq)`` is the idempotent-dedup floor
   (NFR-E04-2); ``UNIQUE (event_id)`` rejects a forged duplicate id. Plus
   ``sync_acks`` (mirroring ``0003_sync_acks.sql``) for the retention watermark.

This module only *shapes* the schema; ``commit.py`` enforces the protocol on
writes. There is no RLS here: on the self-hosted server the gateway/auth layer
binds the caller to a member and the shared ``verify_event`` authorises every
write, so authorisation lives in the validator core, not in database policy
(the deliberate self-host posture, D-31 — token/grant auth, no JWT/RLS,
OIDC deferred per DEBT-14).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

# The syncable-collection allowlist, kept in lock-step with
# supabase/migrations/0002_sync_events.sql (and the local applier's
# SYNCABLE_MODELS + COLLECTION_META). A divergence here is a self-host that
# accepts an event Supabase would refuse — the parity gate's whole point.
_SYNCABLE_COLLECTIONS = (
    "workspaces",
    "projects",
    "tickets",
    "comments",
    "ticket_relationships",
    "members",
    "agent_proposals",
    "memory_entries",
    "memory_links",
    "devices",
    "capability_grants",
    "conflict_records",
)

# A dedicated metadata for the two hand-shaped tables so they create cleanly
# alongside (and after) the ORM tables they reference.
log_metadata = MetaData()

sync_events = Table(
    "sync_events",
    log_metadata,
    Column("revision", BigInteger, Identity(always=True), primary_key=True),
    Column("event_id", String(26), nullable=False),
    Column("collection", String(32), nullable=False),
    Column("entity_id", String(26), nullable=False),
    Column("actor_id", String(26), nullable=False),
    Column("actor_seq", Integer, nullable=False),
    Column("op", String(16), nullable=False),
    Column("base_rev", BigInteger),
    Column("policy_ref", String),
    Column("payload", JSON, nullable=False),
    Column("sig", String),
    # workspace_id references workspaces.id; the FK is added by create_schema via
    # a raw ALTER (the referenced table lives in SQLModel.metadata, a different
    # MetaData, so a Table-level ForeignKey cannot resolve it at create time).
    Column("workspace_id", String(26), nullable=False),
    Column(
        "committed_at",
        # TIMESTAMPTZ NOT NULL DEFAULT now() — matches the Supabase column.
        type_=DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("event_id", name="uq_sync_events_event_id"),
    UniqueConstraint("actor_id", "actor_seq", name="uq_sync_events_actor_seq"),
    CheckConstraint("op IN ('patch', 'append', 'tombstone')", name="ck_sync_events_op"),
    CheckConstraint(
        "collection IN (" + ", ".join(f"'{c}'" for c in _SYNCABLE_COLLECTIONS) + ")",
        name="ck_sync_events_collection",
    ),
    Index("ix_sync_events_collection", "collection", "revision"),
    Index("ix_sync_events_workspace_id", "workspace_id", "revision"),
)

sync_acks = Table(
    "sync_acks",
    log_metadata,
    Column("workspace_id", String(26), nullable=False),
    Column("member_id", String(26), nullable=False),
    Column("replica_id", String(26), nullable=False),
    Column("acked_rev", BigInteger, nullable=False),
    Column(
        "updated_at",
        type_=DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("workspace_id", "member_id", "replica_id", name="uq_sync_acks_replica"),
)


def create_schema(engine: Engine) -> None:
    """Create the full self-hosted schema on a fresh Postgres database.

    Order matters: the ORM tables (``workspaces`` etc.) come first because
    ``sync_events.workspace_id`` references ``workspaces.id``. Idempotent
    (``checkfirst``) so a re-run on an existing database is a no-op — the
    docker-compose entrypoint calls this on boot.
    """
    # Import for the side effect of registering every collection mirror on
    # SQLModel.metadata (devices, capability_grants, members, workspaces, ...).
    import kantaq_db.models  # noqa: F401

    SQLModel.metadata.create_all(engine, checkfirst=True)
    log_metadata.create_all(engine, checkfirst=True)

    # The sync_events → workspaces FK (parity with 0002_sync_events.sql), added
    # here because the referenced table is in a different MetaData. Guarded on
    # pg_constraint so a re-run (the compose entrypoint calls create_schema on
    # every boot) is a no-op.
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = 'fk_sync_events_workspace'")
        ).first()
        if exists is None:
            conn.execute(
                text(
                    "ALTER TABLE sync_events ADD CONSTRAINT fk_sync_events_workspace "
                    "FOREIGN KEY (workspace_id) REFERENCES workspaces (id)"
                )
            )
