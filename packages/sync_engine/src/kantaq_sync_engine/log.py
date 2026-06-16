"""The local append-only event log (E04-T1: FR-E04-1) and the tracker sink.

``EventLogSink`` is what closes the MOD-03 rule "all writes go through the
sync engine as Events": the tracker service emits a ``DomainEvent``, the sink
assigns the protocol envelope (ULID ``event_id``, the actor's next
``actor_seq``) and inserts the log row **on the caller's session**, so the
entity write, its audit row, and its event commit or roll back together.

``actor_seq`` is per-actor and monotonic (D-05: kept from day one so v0.1
signing and v0.2 offline need no rework). The UNIQUE(actor_id, actor_seq)
constraint is the hard floor under every dedup rule in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from sqlite3 import IntegrityError as SQLite3IntegrityError

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from kantaq_core.tracker.events import DomainEvent
from kantaq_db import EventLog, new_ulid
from kantaq_db.meta import COLLECTION_META
from kantaq_protocol import sign
from kantaq_sync_engine.events import Event

# event_log.sync_state vocabulary (MOD-26 §B1, E05-T1). 'pending' is the durable
# outbox; the three terminal states take an event out of the flush loop:
# 'committed' (the backend assigned a revision), 'rejected' (a CAS-rejected
# authoritative_tx or verify-failed event the backend will never accept), and
# 'rebase_required' (a stale agent proposal that must be re-based, not re-pushed).
SYNC_STATE_PENDING = "pending"
SYNC_STATE_COMMITTED = "committed"
SYNC_STATE_REJECTED = "rejected"
SYNC_STATE_REBASE_REQUIRED = "rebase_required"

# The terminal states for an event the backend will never accept. A row in one
# of these is dropped from the fold, so marking it terminal + re-folding the
# entity *reverts* its optimistic local effect (MOD-26 §B1).
_TERMINAL_REJECTED = (SYNC_STATE_REJECTED, SYNC_STATE_REBASE_REQUIRED)


@dataclass(frozen=True)
class EventSigner:
    """The device identity that signs a runtime's events at append (E04-T4).

    ``private_key`` is the device seed (64 lowercase hex, MOD-17); it never
    reprs. ``policy_ref`` is the acting member's live capability-grant id — the
    grant the verified-ingestion path (E24-T5) checks the event against. When
    an ``EventLogSink`` holds a signer, every emitted event carries
    ``base_rev`` + ``policy_ref`` and is Ed25519-signed; when it does not, the
    event is unsigned (solo / pre-cutover / genesis bootstrap).
    """

    private_key: str = field(repr=False)
    policy_ref: str


class SigningRequiredError(Exception):
    """A signed event was required (post-cutover) but no signer was available.

    The local fail-closed invariant of the signing cutover (E04-T4): once a
    runtime signs, it never writes an unsigned event — it raises instead, so a
    missing device key or grant can never silently downgrade to unsigned.
    """


class AuthoritativeWriteError(Exception):
    """An ``authoritative_tx`` collection (grants/tokens) was written through the
    optimistic outbox (MOD-26 §B1).

    Grants and tokens are never queued optimistically: they require a live
    synchronous RPC round-trip and a committed revision before the local row (or
    any derived gateway session) reflects them. Routing one through the
    ``EventLogSink`` would create exactly the offline self-escalation path B1
    forbids — a locally-issued-but-unconfirmed grant the local gateway would
    trust before it syncs (DEBT-15(a)/(b)). The outbox refuses it at the seam.
    """


class AppendOnlyWriteError(Exception):
    """A ``patch`` or ``tombstone`` was written to an ``append_only`` collection
    (MOD-26 §B3).

    Append-only collections (comments) are "created once, never patched": each
    append is a distinct immutable entity, so they never conflict. v0.2 enforces
    that at the write surface rather than assuming it — a ``patch``/``tombstone``
    on an append-only collection is a bug (it would invite a field-merge on a log
    that must stay append-only), so the sink refuses it.
    """


def next_actor_seq(session: Session, actor_id: str) -> int:
    """The actor's next sequence number (1-based, gapless per local log)."""
    last = session.exec(
        select(EventLog.actor_seq)
        .where(EventLog.actor_id == actor_id)
        .order_by(col(EventLog.actor_seq).desc())
        .limit(1)
    ).first()
    return (last or 0) + 1


def entity_base_rev(session: Session, collection: str, entity_id: str) -> int | None:
    """The committed revision this write is based on: the entity's current
    committed head known locally, or ``None`` for a first write (E04-T4).

    Carried on every signed event (FR-E04-6) so the backend records what each
    write saw. v0.0.5/v0.1 resolve conflicts by server commit order (D-05, LWW)
    and do **not** reject on a stale ``base_rev``; optimistic-concurrency
    enforcement is a v0.2 merge-policy concern (the atomic RPC, D-09).
    """
    return session.exec(
        select(EventLog.committed_rev)
        .where(EventLog.collection == collection)
        .where(EventLog.entity_id == entity_id)
        .where(col(EventLog.committed_rev).is_not(None))
        .order_by(col(EventLog.committed_rev).desc())
        .limit(1)
    ).first()


def has_event(session: Session, actor_id: str, actor_seq: int) -> bool:
    return (
        session.exec(
            select(EventLog.event_id)
            .where(EventLog.actor_id == actor_id)
            .where(EventLog.actor_seq == actor_seq)
        ).first()
        is not None
    )


def insert_event(
    session: Session,
    event: Event,
    *,
    committed_rev: int | None = None,
    now: datetime | None = None,
    origin_proposal_id: str | None = None,
) -> EventLog:
    """Append one event row (no commit — the transaction is the caller's).

    ``origin_proposal_id`` (MOD-26 §B3 / E05-T3) tags a ticket write that is the
    apply of an approved agent proposal, so ``flush_outbox`` can bounce a
    stale-and-contending one to ``rebase_required`` instead of minting an
    ordinary conflict_record. Local infra; NULL for every other event.
    """
    row = EventLog(
        event_id=event.event_id,
        collection=event.collection,
        entity_id=event.entity_id,
        actor_id=event.actor_id,
        actor_seq=event.actor_seq,
        op=event.op,
        payload=dict(event.payload),
        base_rev=event.base_rev,
        policy_ref=event.policy_ref,
        sig=event.sig,
        committed_rev=committed_rev,
        # An ingested remote event arrives already committed; a fresh local
        # write starts in the durable outbox as 'pending' (MOD-26 §B1).
        sync_state=(SYNC_STATE_COMMITTED if committed_rev is not None else SYNC_STATE_PENDING),
        origin_proposal_id=origin_proposal_id,
        created_at=now or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def row_to_event(row: EventLog) -> Event:
    return Event(
        event_id=row.event_id,
        collection=row.collection,
        entity_id=row.entity_id,
        actor_id=row.actor_id,
        actor_seq=row.actor_seq,
        op=row.op,  # type: ignore[arg-type]  # the column stores the Op literal
        base_rev=row.base_rev,
        policy_ref=row.policy_ref,
        payload=dict(row.payload),
        sig=row.sig,
    )


def pending_rows(session: Session) -> list[EventLog]:
    """The durable outbox: events the backend has not committed *and* that are
    still flushable, in ``(actor_id, actor_seq)`` order (MOD-26 §B1).

    An event whose ``sync_state`` has left ``'pending'`` (a backend-rejected or
    rebase-required event) stays out of the outbox so the flush loop never
    re-pushes it forever — that closes the zombie-event / stuck-``pending_count``
    hole. Until terminal states are written every row is ``'pending'``, so this
    is behaviour-preserving over the old ``committed_rev IS NULL`` query.
    """
    rows = session.exec(
        select(EventLog)
        .where(col(EventLog.committed_rev).is_(None))
        .where(EventLog.sync_state == SYNC_STATE_PENDING)
    ).all()
    return sorted(rows, key=lambda r: (r.actor_id, r.actor_seq))


def entity_rows(session: Session, collection: str, entity_id: str) -> list[EventLog]:
    """One entity's events in resolution order: commit order, pending last.

    This *is* D-05: the backend's commit order decides ties; local events the
    backend has not committed yet apply after everything committed (they are
    the optimistic tail). Terminal-rejected rows are excluded, so re-folding an
    entity after marking one of its events ``rejected``/``rebase_required``
    drops that event's optimistic effect (the revert path, MOD-26 §B1).
    """
    rows = session.exec(
        select(EventLog)
        .where(EventLog.collection == collection)
        .where(EventLog.entity_id == entity_id)
        .where(col(EventLog.sync_state).not_in(_TERMINAL_REJECTED))
    ).all()
    return sorted(rows, key=_resolution_key)


def collection_rows(session: Session, collection: str | None = None) -> list[EventLog]:
    statement = select(EventLog).where(col(EventLog.sync_state).not_in(_TERMINAL_REJECTED))
    if collection is not None:
        statement = statement.where(EventLog.collection == collection)
    return sorted(session.exec(statement).all(), key=_resolution_key)


def event_row(session: Session, actor_id: str, actor_seq: int) -> EventLog | None:
    """The local log row for one ``(actor_id, actor_seq)``, or ``None``."""
    return session.exec(
        select(EventLog).where(EventLog.actor_id == actor_id).where(EventLog.actor_seq == actor_seq)
    ).one_or_none()


def event_by_id(session: Session, event_id: str) -> EventLog | None:
    """The local log row for one ``event_id`` (the primary key), or ``None`` —
    how a ``CommitResult`` (keyed by event_id) finds the row to mark committed."""
    return session.get(EventLog, event_id)


def mark_proposal_origin(session: Session, actor_id: str, actor_seq: int, proposal_id: str) -> None:
    """Tag the event at ``(actor_id, actor_seq)`` as the apply of a proposal
    (MOD-26 §B3 / E05-T3).

    Called right after a proposal's ticket write is emitted so ``flush_outbox``
    can route a stale-and-contending one to ``rebase_required`` rather than an
    ordinary conflict_record. No-op if the row is absent (the write produced no
    event, e.g. a no-change diff)."""
    row = event_row(session, actor_id, actor_seq)
    if row is not None:
        row.origin_proposal_id = proposal_id
        session.add(row)
        session.flush()


def _resolution_key(row: EventLog) -> tuple[int, int, str, int]:
    committed = row.committed_rev
    if committed is not None:
        return (0, committed, "", 0)
    return (1, 0, row.actor_id, row.actor_seq)


class DuplicateEventError(Exception):
    """An (actor_id, actor_seq) pair was appended twice (NFR-E04-2 violation)."""


class EventLogSink:
    """`kantaq_core.tracker.EventSink` implementation over the local log.

    Bound to one session and one acting member; the runtime constructs one per
    request so events ride the request transaction and are attributed to the
    authenticated actor.

    With a ``signer`` (E04-T4) every emitted event is signed at append: it
    carries ``base_rev`` (the entity's committed head) and ``policy_ref`` (the
    signer's capability grant), then an Ed25519 signature over the MOD-17
    signing bytes — set *before* signing, so both are covered. ``require_signed``
    enforces the cutover's local fail-closed rule: an emit with no signer raises
    ``SigningRequiredError`` rather than writing an unsigned row.
    """

    def __init__(
        self,
        session: Session,
        actor_id: str,
        *,
        signer: EventSigner | None = None,
        require_signed: bool = False,
    ) -> None:
        if require_signed and signer is None:
            raise SigningRequiredError(
                "signing is required (post-cutover) but no device signer was provided"
            )
        self._session = session
        self._actor_id = actor_id
        self._signer = signer

    def emit(self, event: DomainEvent) -> None:
        # authoritative_tx (grants/tokens) is synchronous-only: it can never
        # enter the optimistic outbox (MOD-26 §B1). Refuse it at the seam so a
        # locally-issued-but-unconfirmed grant can never be queued offline.
        meta = COLLECTION_META.get(event.collection)
        if meta is not None and meta.merge_policy == "authoritative_tx":
            raise AuthoritativeWriteError(
                f"{event.collection!r} is authoritative_tx — it commits synchronously "
                "via the atomic RPC, never as an optimistic outbox event (MOD-26 §B1)"
            )
        # append_only (comments) is created once, never patched: a patch/tombstone
        # would invite a field-merge on a log that must stay append-only. Only an
        # 'append' op is allowed (MOD-26 §B3, "enforced, not assumed").
        if meta is not None and meta.merge_policy == "append_only" and event.op != "append":
            raise AppendOnlyWriteError(
                f"{event.collection!r} is append_only — only an 'append' op is allowed, "
                f"not {event.op!r} (MOD-26 §B3)"
            )
        signer = self._signer
        protocol_event = Event(
            event_id=new_ulid(),
            collection=event.collection,
            entity_id=event.entity_id,
            actor_id=self._actor_id,
            actor_seq=next_actor_seq(self._session, self._actor_id),
            op=event.op,
            base_rev=(
                entity_base_rev(self._session, event.collection, event.entity_id)
                if signer is not None
                else None
            ),
            policy_ref=signer.policy_ref if signer is not None else None,
            payload=dict(event.payload),
        )
        if signer is not None:
            protocol_event = sign(protocol_event, signer.private_key)
        try:
            insert_event(self._session, protocol_event)
        except (IntegrityError, SQLite3IntegrityError) as exc:  # pragma: no cover - racy path
            raise DuplicateEventError(
                f"actor_seq collision for {self._actor_id}: {protocol_event.actor_seq}"
            ) from exc
