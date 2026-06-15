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

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_db import SyncCursor
from kantaq_sync_engine import log as event_log
from kantaq_sync_engine.apply import refold_entity
from kantaq_sync_engine.events import BackendPort, BackendUnavailable, Event
from kantaq_sync_engine.verify import EventRejected

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


@dataclass(frozen=True)
class Backoff:
    """Bounded exponential backoff for the offline-aware flush loop (B1).

    A transport failure must never spin: each retry waits longer, capped, for a
    bounded number of attempts. Past the cap the outbox stays durable and the
    next ``flush_outbox`` call retries — the write is never lost.
    """

    base_seconds: float = 0.5
    factor: float = 2.0
    cap_seconds: float = 30.0
    max_attempts: int = 6

    def delay(self, attempt: int) -> float:
        """Seconds to wait before retry ``attempt`` (1-based)."""
        return min(self.cap_seconds, self.base_seconds * (self.factor ** (attempt - 1)))


@dataclass(frozen=True)
class FlushResult:
    submitted: int
    committed: int
    reconciled: int  # own committed events backfilled before pushing (dropped-ack)
    rejected: int  # events moved to a terminal state (verify-failed / never-acceptable)
    attempts: int  # connectivity attempts made
    drained: bool  # the outbox is empty afterwards (no pending rows remain)


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

    def apply_inbox(self, collection: str | None = None) -> PullResult:
        """The durable inbox (MOD-26 §B2): the named entry point for ingest.

        Ingests committed events since the cursor, re-folds what they touch, and
        advances the cursor — all in one transaction, so a dropped connection
        rolls back and the retry re-pulls from the old cursor (dedup-safe). This
        names the two §8.2 modes that both ride ``pull``: ``snapshot_then_stream``
        (first sync / disaster recovery — ``snapshot`` then this) and
        ``resume_stream`` (every reconnect — ``pull(since=cursor)``). Trust-root
        events route to the dedicated identity ingest (``apply.ingest_trust_root``),
        never the domain fold, so an unscoped pull over a backend holding
        device/grant events ingests the trust root without wedging (DEBT-21).
        """
        return self.pull(collection=collection)

    def ack(self, cursor: int, collection: str | None = None) -> None:
        """Persist a cursor explicitly (the pull loop normally does this)."""
        with Session(self._db) as session:
            self._ack(session, collection or ALL_COLLECTIONS, cursor)
            session.commit()

    # --------------------------------------------------------------- outbox

    def flush_outbox(
        self,
        *,
        backoff: Backoff | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> FlushResult:
        """Drain the durable outbox with offline-aware bounded backoff (B1).

        On reconnect this *first* reconciles dropped acks — backfills the commit
        order of our own events the backend already holds — so a connection that
        dropped after the server committed but before we recorded the ack never
        re-pushes (exactly-once, NFR-E05-1) or mis-orders the pending tail. Then
        it submits the remaining pending events in ``(actor_id, actor_seq)``
        order.

        A transport failure (``BackendUnavailable``) backs off and retries up to
        ``backoff.max_attempts``; the events stay durably in the outbox, so a
        partition never strands a write. A per-event rejection (a verify-failed
        or otherwise never-acceptable event) is moved to a terminal ``sync_state``
        and its optimistic effect reverted, so it leaves the outbox instead of
        being re-pushed forever (no zombie retry, no stuck ``pending_count``).
        """
        backoff = backoff or Backoff()
        sleep = sleeper if sleeper is not None else time.sleep
        attempts = 0
        while True:
            attempts += 1
            try:
                with Session(self._db) as session:
                    reconciled = self._reconcile_dropped_acks(session)
                    submitted, committed, rejected = self._drain(session)
                    drained = not event_log.pending_rows(session)
                    session.commit()
                return FlushResult(submitted, committed, reconciled, rejected, attempts, drained)
            except BackendUnavailable:
                if attempts >= backoff.max_attempts:
                    with Session(self._db) as session:
                        drained = not event_log.pending_rows(session)
                    return FlushResult(0, 0, 0, 0, attempts, drained)
                sleep(backoff.delay(attempts))

    def _reconcile_dropped_acks(self, session: Session) -> int:
        """Backfill ``committed_rev`` for our own pending events the backend
        already holds (the dropped-ack hole, B1) and re-fold what they touch, so
        the entity ends in commit order — identical to a replica that pulled it.
        Idempotent; advances no cursor (the inbox owns the cursor)."""
        cursor = self._cursor(session, ALL_COLLECTIONS)
        touched: list[tuple[str, str]] = []
        backfilled = 0
        for entry in self._backend.pull(collection=None, since=cursor):
            event = entry.event
            if event.actor_id != self._actor_id:
                continue  # other actors' events are the inbox's job, not the outbox
            row = event_log.event_row(session, event.actor_id, event.actor_seq)
            if row is not None and row.committed_rev is None:
                row.committed_rev = entry.revision
                row.sync_state = event_log.SYNC_STATE_COMMITTED
                session.add(row)
                if (event.collection, event.entity_id) not in touched:
                    touched.append((event.collection, event.entity_id))
                backfilled += 1
        for collection, entity_id in touched:
            refold_entity(session, collection, entity_id)
        return backfilled

    def _drain(self, session: Session) -> tuple[int, int, int]:
        """Push pending events; resolve per-event rejections to terminal states.

        A ``VerifyingBackend`` rejection (``EventRejected``) names the one
        offending event: it is marked terminal, its optimistic effect reverted,
        and the remaining events re-pushed — so a single never-acceptable event
        cannot wedge the outbox. A transport failure propagates to the backoff
        loop, rolling the whole attempt back (the dropped-ack reconcile then
        re-derives any commit the server did record).
        """
        submitted = committed = rejected = 0
        while True:
            pending = event_log.pending_rows(session)
            if not pending:
                break
            events = [event_log.row_to_event(row) for row in pending]
            try:
                results = self._backend.push(events)
            except EventRejected as exc:
                self._reject(session, exc.event, exc.code, exc.reason)
                rejected += 1
                continue  # poison event removed from the outbox; re-drain the rest
            submitted = len(events)
            for entry in results:
                self._mark_committed(session, entry.event, entry.revision)
            committed = len(results)
            break
        return submitted, committed, rejected

    def _reject(self, session: Session, event: Event, code: str, reason: str) -> None:
        """Move a never-acceptable event to a terminal state and revert its
        optimistic local effect (B1). The row keeps its ``actor_seq`` slot in the
        log but leaves the outbox and the fold; the revert is the re-fold (the
        rejected row is excluded from ``entity_rows``)."""
        row = event_log.event_row(session, event.actor_id, event.actor_seq)
        if row is None:
            return
        row.sync_state = event_log.SYNC_STATE_REJECTED
        session.add(row)
        session.flush()
        refold_entity(session, event.collection, event.entity_id)
        audit.write(
            session,
            actor_id=event.actor_id,
            action=f"{event.collection.rstrip('s')}.sync_rejected"[:64],
            source="sync",
            object_ref=f"{event.collection}/{event.entity_id}",
            after={"code": code, "reason": reason, "event_id": event.event_id},
        )

    # ----------------------------------------------------------------- state

    def cursor(self, collection: str | None = None) -> int:
        with Session(self._db) as session:
            return self._cursor(session, collection or ALL_COLLECTIONS)

    def pending_count(self) -> int:
        with Session(self._db) as session:
            return len(event_log.pending_rows(session))

    # --------------------------------------------------------------- helpers

    def _mark_committed(self, session: Session, event: Event, revision: int) -> None:
        row = event_log.event_row(session, event.actor_id, event.actor_seq)
        if row is not None and row.committed_rev is None:
            row.committed_rev = revision
            row.sync_state = event_log.SYNC_STATE_COMMITTED
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
