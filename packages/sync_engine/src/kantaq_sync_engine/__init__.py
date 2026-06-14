"""kantaq sync engine: event log, push/pull, snapshots (MOD-04 / Epic E04).

v0.0.5 is online sync: an append-only local event log (``log``), idempotent
push/pull against a backend port with last-writer-wins by commit order
(``engine``, D-05), entity state as the fold of the log (``apply``), and
deterministic NDJSON snapshots (``snapshot``). Signing lands v0.1 (MOD-17);
offline outbox and conflict records land v0.2 (MOD-26).
"""

from __future__ import annotations

from kantaq_sync_engine.apply import (
    SYNCABLE_MODELS,
    UnknownCollectionError,
    folded_fields,
    refold_entity,
)
from kantaq_sync_engine.engine import (
    ALL_COLLECTIONS,
    PullResult,
    PushResult,
    SyncEngine,
)
from kantaq_sync_engine.events import BackendPort, CommittedEvent, Event, Op, fold_events
from kantaq_sync_engine.log import (
    DuplicateEventError,
    EventLogSink,
    EventSigner,
    SigningRequiredError,
    collection_rows,
    entity_base_rev,
    entity_rows,
    has_event,
    insert_event,
    next_actor_seq,
    pending_rows,
    row_to_event,
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
    "SYNCABLE_MODELS",
    "UNSIGNED",
    "VERIFY_OK",
    "BackendPort",
    "CommittedEvent",
    "DuplicateEventError",
    "Event",
    "EventLogSink",
    "EventRejected",
    "EventSigner",
    "EventVerification",
    "Op",
    "PullResult",
    "PushResult",
    "SigningRequiredError",
    "SyncEngine",
    "UnknownCollectionError",
    "VerifyContext",
    "VerifyingBackend",
    "__version__",
    "collection_rows",
    "compose_snapshot",
    "entity_base_rev",
    "entity_rows",
    "fold_collection",
    "fold_events",
    "folded_fields",
    "has_event",
    "insert_event",
    "next_actor_seq",
    "parse_snapshot",
    "pending_rows",
    "refold_entity",
    "row_to_event",
    "verify_event",
]
