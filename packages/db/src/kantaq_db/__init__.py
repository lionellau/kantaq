"""kantaq db: SQLModel models + Alembic migrations (MOD-02 / Epic E02).

One schema definition, two stores: the models in ``models`` compile to both local
SQLite and Supabase Postgres from the same metadata (D-07). ``migrations`` drives
Alembic up/down; ``schema_version`` is the boot guard; ``parity`` proves the two
dialects agree; ``seed`` populates a demo workspace; ``meta`` carries the
per-collection protocol metadata the sync layer (E03/E04) will read.
"""

from __future__ import annotations

from kantaq_db.ids import is_ulid, new_ulid, ulid_timestamp_ms
from kantaq_db.meta import COLLECTION_META, CollectionMeta, PrivacyClass, collection_names
from kantaq_db.models import (
    COLLECTION_MODELS,
    AgentProposal,
    AuditEvent,
    CapabilityGrantRow,
    Comment,
    Device,
    EventLog,
    LocalSetting,
    Member,
    MemoryEntry,
    MemoryLink,
    Project,
    SchemaVersion,
    SyncCursor,
    TelemetryEvent,
    Ticket,
    TicketRelationship,
    Token,
    Workspace,
)

__version__: str = "0.0.5"

__all__ = [
    "COLLECTION_META",
    "COLLECTION_MODELS",
    "AgentProposal",
    "AuditEvent",
    "CapabilityGrantRow",
    "Comment",
    "CollectionMeta",
    "Device",
    "EventLog",
    "LocalSetting",
    "Member",
    "MemoryEntry",
    "MemoryLink",
    "PrivacyClass",
    "Project",
    "SchemaVersion",
    "SyncCursor",
    "TelemetryEvent",
    "Ticket",
    "TicketRelationship",
    "Token",
    "Workspace",
    "__version__",
    "collection_names",
    "is_ulid",
    "new_ulid",
    "ulid_timestamp_ms",
]
