"""Online push/pull against a backend port (E04-T2: FR-E04-2, FR-E04-3).

The v0.0.5 loop is deliberately simple (sprint risk note): online only, last
writer wins by the backend's commit order, no offline outbox, no conflict
records. What it does guarantee:

- **Idempotent re-push** (NFR-E04-2): pending events are submitted in actor
  order; the backend dedups by ``(actor_id, actor_seq)``. An event the backend
  already holds simply gets its ``committed_rev`` reconciled on the next pull.
- **Convergent ingest**: a pulled event is recorded in the local log, then the
  touched entity is *re-folded* from the log in commit order (see ``apply``),
  so out-of-order arrivals cannot leave replicas disagreeing.
- **Crash-safe cursors**: the cursor row only advances in the same transaction
  that ingested the batch. A connection dropped mid-pull re-pulls from the old
  cursor and the log dedup makes the overlap harmless.

Every ingested remote event writes one audit row attributed to the *original*
actor with ``source="sync"`` (MOD-07's vocabulary for the engine replaying).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_db import SyncCursor
from kantaq_sync_engine import log as event_log
from kantaq_sync_engine.apply import refold_entity
from kantaq_sync_engine.events import BackendPort, Event

ALL_COLLECTIONS = "*"


@dataclass(frozen=True)
class PushResult:
    submitted: int
    committed: int
    already_known: int


@dataclass(frozen=True)
class PullResult:
    received: int
    applied: int
    own_reconciled: int
    cursor: int


class SyncEngine:
    """One replica's sync loop: a local log, a backend port, an actor."""

    def __init__(self, db_engine: Engine, backend: BackendPort, *, actor_id: str) -> None:
        self._db = db_engine
        self._backend = backend
        self._actor_id = actor_id

    # ------------------------------------------------------------------ push

    def push(self) -> PushResult:
        """Submit pending local events; mark what the backend committed."""
        with Session(self._db) as session:
            pending = event_log.pending_rows(session)
            events = [event_log.row_to_event(row) for row in pending]
            committed = self._backend.push(events)
            for entry in committed:
                self._mark_committed(session, entry.event, entry.revision)
            session.commit()
        return PushResult(
            submitted=len(events),
            committed=len(committed),
            already_known=len(events) - len(committed),
        )

    # ------------------------------------------------------------------ pull

    def pull(self, collection: str | None = None) -> PullResult:
        """Ingest committed events since the cursor; re-fold what they touch."""
        key = collection or ALL_COLLECTIONS
        with Session(self._db) as session:
            since = self._cursor(session, key)
            batch = self._backend.pull(collection=collection, since=since)

            applied = 0
            own = 0
            touched: list[tuple[str, str]] = []
            highest = since
            for entry in batch:
                event = entry.event
                highest = max(highest, entry.revision)
                if event_log.has_event(session, event.actor_id, event.actor_seq):
                    # Ours (or already ingested): reconcile the commit order.
                    self._mark_committed(session, event, entry.revision)
                    own += 1
                    continue
                event_log.insert_event(session, event, committed_rev=entry.revision)
                if (event.collection, event.entity_id) not in touched:
                    touched.append((event.collection, event.entity_id))
                audit.write(
                    session,
                    actor_id=event.actor_id,
                    action=f"{event.collection.rstrip('s')}.sync",
                    source="sync",
                    object_ref=f"{event.collection}/{event.entity_id}",
                    after=dict(event.payload),
                )
                applied += 1

            for touched_collection, entity_id in touched:
                refold_entity(session, touched_collection, entity_id)

            # Ack rides the ingest transaction: cursor and log move together.
            self._ack(session, key, highest)
            session.commit()
        return PullResult(received=len(batch), applied=applied, own_reconciled=own, cursor=highest)

    def ack(self, cursor: int, collection: str | None = None) -> None:
        """Persist a cursor explicitly (the pull loop normally does this)."""
        with Session(self._db) as session:
            self._ack(session, collection or ALL_COLLECTIONS, cursor)
            session.commit()

    # ----------------------------------------------------------------- state

    def cursor(self, collection: str | None = None) -> int:
        with Session(self._db) as session:
            return self._cursor(session, collection or ALL_COLLECTIONS)

    def pending_count(self) -> int:
        with Session(self._db) as session:
            return len(event_log.pending_rows(session))

    # --------------------------------------------------------------- helpers

    def _mark_committed(self, session: Session, event: Event, revision: int) -> None:
        from sqlmodel import select

        from kantaq_db import EventLog

        row = session.exec(
            select(EventLog)
            .where(EventLog.actor_id == event.actor_id)
            .where(EventLog.actor_seq == event.actor_seq)
        ).one_or_none()
        if row is not None and row.committed_rev is None:
            row.committed_rev = revision
            session.add(row)

    def _cursor(self, session: Session, key: str) -> int:
        row = session.get(SyncCursor, (key, self._actor_id))
        return row.acked_rev if row is not None else 0

    def _ack(self, session: Session, key: str, revision: int) -> None:
        row = session.get(SyncCursor, (key, self._actor_id))
        if row is None:
            row = SyncCursor(collection=key, actor_id=self._actor_id, acked_rev=revision)
        elif revision > row.acked_rev:
            row.acked_rev = revision
        else:
            return
        row.updated_at = datetime.now(UTC)
        session.add(row)
        session.flush()
