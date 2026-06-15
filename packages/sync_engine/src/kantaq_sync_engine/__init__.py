"""kantaq sync engine: event log, push/pull, snapshots (MOD-04 / Epic E04).

v0.0.5 is online sync: an append-only local event log (``log``), idempotent
push/pull against a backend port with last-writer-wins by commit order
(``engine``, D-05), entity state as the fold of the log (``apply``), and
deterministic NDJSON snapshots (``snapshot``). Signing lands v0.1 (MOD-17);
offline outbox and conflict records land v0.2 (MOD-26).
"""

from __future__ import annotations

from kantaq_sync_engine.apply import (
    DOMAIN_MODELS,
    SYNCABLE_MODELS,
    TRUST_ROOT_MODELS,
    UnknownCollectionError,
    folded_fields,
    ingest_trust_root,
    refold_entity,
)
from kantaq_sync_engine.engine import (
    ALL_COLLECTIONS,
    Backoff,
    FlushResult,
    PullResult,
    PushResult,
    ResolveResult,
    SyncEngine,
)
from kantaq_sync_engine.events import (
    BackendPort,
    BackendUnavailable,
    CommitResult,
    CommittedEvent,
    Event,
    FieldConflict,
    Op,
    fold_events,
)
from kantaq_sync_engine.log import (
    SYNC_STATE_COMMITTED,
    SYNC_STATE_PENDING,
    SYNC_STATE_REBASE_REQUIRED,
    SYNC_STATE_REJECTED,
    AuthoritativeWriteError,
    DuplicateEventError,
    EventLogSink,
    EventSigner,
    SigningRequiredError,
    collection_rows,
    entity_base_rev,
    entity_rows,
    event_by_id,
    event_row,
    has_event,
    insert_event,
    next_actor_seq,
    pending_rows,
    row_to_event,
)
from kantaq_sync_engine.merge import (
    ENTITY_FIELD,
    FieldDecision,
    MergeOutcome,
    conflict_record_id,
    detect_merge,
)
from kantaq_sync_engine.snapshot import compose_snapshot, fold_collection, parse_snapshot
from kantaq_sync_engine.verify import (
    INVALID_SIGNATURE,
    POLICY_DENIED,
    SCHEMA_VIOLATION,
    STALE_BASE_REV,
    UNSIGNED,
    VERIFY_OK,
    EventRejected,
    EventVerification,
    VerifyContext,
    VerifyingBackend,
    verify_event,
)

__version__: str = "0.1.0"

__all__ = [
    "ALL_COLLECTIONS",
    "INVALID_SIGNATURE",
    "POLICY_DENIED",
    "SCHEMA_VIOLATION",
    "STALE_BASE_REV",
    "DOMAIN_MODELS",
    "SYNCABLE_MODELS",
    "TRUST_ROOT_MODELS",
    "SYNC_STATE_COMMITTED",
    "SYNC_STATE_PENDING",
    "SYNC_STATE_REBASE_REQUIRED",
    "SYNC_STATE_REJECTED",
    "UNSIGNED",
    "VERIFY_OK",
    "AuthoritativeWriteError",
    "Backoff",
    "BackendPort",
    "BackendUnavailable",
    "CommitResult",
    "CommittedEvent",
    "DuplicateEventError",
    "ENTITY_FIELD",
    "Event",
    "EventLogSink",
    "EventRejected",
    "EventSigner",
    "EventVerification",
    "FieldConflict",
    "FieldDecision",
    "FlushResult",
    "MergeOutcome",
    "Op",
    "PullResult",
    "PushResult",
    "ResolveResult",
    "SigningRequiredError",
    "SyncEngine",
    "UnknownCollectionError",
    "VerifyContext",
    "VerifyingBackend",
    "__version__",
    "collection_rows",
    "compose_snapshot",
    "conflict_record_id",
    "detect_merge",
    "entity_base_rev",
    "entity_rows",
    "event_by_id",
    "event_row",
    "fold_events",
    "fold_collection",
    "folded_fields",
    "has_event",
    "ingest_trust_root",
    "insert_event",
    "next_actor_seq",
    "parse_snapshot",
    "pending_rows",
    "refold_entity",
    "row_to_event",
    "verify_event",
]
