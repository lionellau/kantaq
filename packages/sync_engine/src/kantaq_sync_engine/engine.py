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
from kantaq_db import AgentProposal, ConflictRecord, EventLog, SyncCursor, new_ulid
from kantaq_db.schema_version import EXPECTED_SCHEMA_VERSION
from kantaq_protocol import sign
from kantaq_sync_engine import log as event_log
from kantaq_sync_engine.apply import refold_entity
from kantaq_sync_engine.events import (
    SYNC_VERSION,
    BackendPort,
    BackendUnavailable,
    CommitResult,
    Event,
    RebaseRequired,
    SessionInit,
    SyncVersionUnsupported,
)
from kantaq_sync_engine.log import EventSigner
from kantaq_sync_engine.merge import conflict_record_id
from kantaq_sync_engine.verify import EventRejected

ALL_COLLECTIONS = "*"

# Agent-proposal staleness policy (MOD-26 §B3 / E05-T3). When a human approves an
# agent proposal whose ticket write turns out to be based on a revision the team
# moved past, this decides whether to bounce the proposal back for re-decision:
# - 'auto_rebase' (default, smooth UX): bounce only when the proposal's OWN fields
#   genuinely contend (the agent's value would clobber a committed change). A
#   stale-but-non-conflicting proposal (it touched a different field) just
#   auto-merges, so the human is never nagged for a needless re-approval.
# - 'strict_rebase' (conservative): bounce on ANY stale base — re-confirm every
#   proposal that raced any change since it was made.
# Either way a contended field is restored to the team's committed head and the
# agent's stale value never silently wins; the proposal's diff is preserved for
# the human to re-apply.
PROPOSAL_POLICY_AUTO_REBASE = "auto_rebase"
PROPOSAL_POLICY_STRICT_REBASE = "strict_rebase"
PROPOSAL_STALE_POLICIES = (PROPOSAL_POLICY_AUTO_REBASE, PROPOSAL_POLICY_STRICT_REBASE)


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
    stale: int  # committed but base_rev was stale (a concurrent write landed first)
    minted: int  # conflict_records minted from per-field conflicts (E05-T2)
    rebased: int  # stale agent proposals bounced to rebase_required (E05-T3)
    attempts: int  # connectivity attempts made
    drained: bool  # the outbox is empty afterwards (no pending rows remain)


@dataclass(frozen=True)
class ResolveResult:
    conflict_id: str
    resolved: bool  # the record is now resolved (the superseding write committed)
    rebase_required: bool  # the field head moved past head_rev → record re-surfaces


class SyncEngine:
    """One replica's sync loop: a local log, a backend port, an actor."""

    # Tolerate one version of skew either way (MOD-26 §B7): during a staggered
    # team rollout an N and an N+1 replica must still converge; N±2 is refused.
    SYNC_VERSION_SKEW = 1

    def __init__(
        self,
        db_engine: Engine,
        backend: BackendPort,
        *,
        actor_id: str,
        workspace_id: str | None = None,
        signer: EventSigner | None = None,
    ) -> None:
        self._db = db_engine
        self._backend = backend
        self._actor_id = actor_id
        # workspace_id + signer are needed only to MINT conflict_records (E05-T2):
        # the record carries the workspace it scopes, and post-cutover the minted
        # event is signed under the actor's conflict_records.write grant. A bare
        # sync loop (no workspace_id) skips minting.
        self._workspace_id = workspace_id
        self._signer = signer
        # The §B7 handshake result, memoized for the session (one negotiation,
        # not one-per-call). Reset to None to re-negotiate (e.g. after reconnect).
        self._session: SessionInit | None = None

    # ----------------------------------------------------------- handshake

    def negotiate_session(self) -> SessionInit:
        """Exchange protocol versions and verify the peer is within ±1 (§B7).

        Memoized for the session. A backend without ``session_init`` (a
        pre-handshake transport) is treated as same-version — negotiation is
        skipped, preserving backward compatibility. On an out-of-range peer this
        writes a denial audit row and raises ``SyncVersionUnsupported`` **before**
        any drain/ingest, so the durable log is never touched.
        """
        if self._session is not None:
            return self._session
        init = getattr(self._backend, "session_init", None)
        if init is None:
            self._session = SessionInit(SYNC_VERSION, EXPECTED_SCHEMA_VERSION)
            return self._session
        peer: SessionInit = init(sync_version=SYNC_VERSION, schema_version=EXPECTED_SCHEMA_VERSION)
        if abs(peer.sync_version - SYNC_VERSION) > self.SYNC_VERSION_SKEW:
            self._audit_version_reject(peer)
            raise SyncVersionUnsupported(
                f"backend sync_version {peer.sync_version} is more than "
                f"{self.SYNC_VERSION_SKEW} from ours ({SYNC_VERSION}); refusing to sync"
            )
        self._session = peer
        return peer

    def _audit_version_reject(self, peer: SessionInit) -> None:
        """Record the §B7 refusal (NFR-E09-1 style) in its own transaction."""
        with Session(self._db) as session:
            audit.write(
                session,
                actor_id=self._actor_id,
                action="sync.version_rejected",
                source="sync",
                object_ref="sync/session",
                after={
                    "ours": {"sync": SYNC_VERSION, "schema": EXPECTED_SCHEMA_VERSION},
                    "peer": {"sync": peer.sync_version, "schema": peer.schema_version},
                },
            )
            session.commit()

    # ------------------------------------------------------------------ push

    def push(self) -> PushResult:
        """Submit pending local events; mark what the backend committed."""
        self.negotiate_session()
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
        self.negotiate_session()
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
        proposal_stale_policy: str = PROPOSAL_POLICY_AUTO_REBASE,
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

        ``proposal_stale_policy`` (MOD-26 §B3 / E05-T3) decides whether a stale
        agent-proposal ticket write is bounced to ``rebase_required`` — the
        runtime passes the workspace setting; ``auto_rebase`` (default) bounces
        only on a genuine field clash.
        """
        if proposal_stale_policy not in PROPOSAL_STALE_POLICIES:
            raise ValueError(f"unknown proposal_stale_policy {proposal_stale_policy!r}")
        backoff = backoff or Backoff()
        sleep = sleeper if sleeper is not None else time.sleep
        self.negotiate_session()  # §B7: refuse an out-of-range peer before draining
        attempts = 0
        while True:
            attempts += 1
            try:
                with Session(self._db) as session:
                    reconciled = self._reconcile_dropped_acks(session)
                    submitted, committed, rejected, stale, minted, rebased = self._drain(
                        session, proposal_stale_policy
                    )
                    drained = not event_log.pending_rows(session)
                    session.commit()
                return FlushResult(
                    submitted,
                    committed,
                    reconciled,
                    rejected,
                    stale,
                    minted,
                    rebased,
                    attempts,
                    drained,
                )
            except BackendUnavailable:
                if attempts >= backoff.max_attempts:
                    with Session(self._db) as session:
                        drained = not event_log.pending_rows(session)
                    return FlushResult(0, 0, 0, 0, 0, 0, 0, attempts, drained)
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

    def _drain(
        self, session: Session, proposal_stale_policy: str = PROPOSAL_POLICY_AUTO_REBASE
    ) -> tuple[int, int, int, int, int, int]:
        """Commit pending events through the atomic RPC (the DEBT-25 cutover) and
        map each ``CommitResult`` to its local row.

        A ``VerifyingBackend`` rejection (``EventRejected``) names the one
        offending event: it is marked terminal, its optimistic effect reverted,
        and the remaining events re-committed — so a single never-acceptable
        event cannot wedge the outbox. A transport failure propagates to the
        backoff loop, rolling the whole attempt back (the dropped-ack reconcile
        then re-derives any commit the server did record). A ``stale_base_rev``
        result means the event committed LWW-by-order but a concurrent write
        landed first.

        Writes split by origin (MOD-26 §B3/B4):
        - **ordinary** writes commit together (commit-and-flag, ride-flagged);
          one that contends mints a ``conflict_record`` (the field rides its LWW
          value, the record is the human's audit-and-correct surface);
        - an **agent-proposal** ticket write (``origin_proposal_id`` set) commits
          as a per-proposal **CAS** (``cas=True``): if it would contend the RPC
          commits nothing and raises ``RebaseRequired`` — the agent's stale value
          never lands, the proposal flips to ``rebase_required`` and the write
          leaves the outbox (the §8.5 propose-first rule). No committed write to
          undo means no partition window: an interrupted bounce just re-submits
          and re-rejects until the base is fresh.
        """
        submitted = committed = rejected = stale = minted = rebased = 0
        while True:
            pending = event_log.pending_rows(session)
            if not pending:
                break
            ordinary = [r for r in pending if r.origin_proposal_id is None]
            proposal_rows = [r for r in pending if r.origin_proposal_id is not None]
            # Commit the ordinary batch FIRST — it carries the proposal's own
            # status=approved flip. A bounce (below) then emits status=rebase_required
            # AFTER it, so the flip supersedes 'approved' in commit order (else the
            # later-committed 'approved' would LWW-overwrite the bounce).
            if ordinary:
                events = [event_log.row_to_event(row) for row in ordinary]
                try:
                    results = self._backend.commit_events(events)
                except EventRejected as exc:
                    self._reject(session, exc.event, exc.code, exc.reason)
                    rejected += 1
                    continue  # poison event removed from the outbox; re-drain the rest
                submitted += len(events)
                for result in results:
                    self._mark_committed_by_id(session, result.event_id, result.revision)
                    if result.status == "committed":
                        committed += 1
                    if result.stale_base_rev is not None:
                        stale += 1
                    if result.conflicts:
                        minted += self._mint_conflict_records(session, result)
            # Then each proposal's ticket write as its own CAS, so a rebase-reject
            # aborts only that write (not the whole flush) and the rebase flip
            # lands after the approved flip.
            for row in proposal_rows:
                submitted += 1
                rebased += self._commit_proposal_write(session, row, proposal_stale_policy)
            break
        return submitted, committed, rejected, stale, minted, rebased

    def _commit_proposal_write(self, session: Session, row: EventLog, policy: str) -> int:
        """Commit one approved-proposal ticket write as a CAS (MOD-26 §B3).

        ``auto_rebase`` (default): the write is a compare-and-swap — a genuine
        field clash raises ``RebaseRequired`` (nothing committed) and the proposal
        is bounced; a stale-but-non-conflicting write (different field) auto-merges
        and commits. ``strict_rebase``: additionally re-confirm a non-conflicting
        but stale write (it commits, then the proposal is flagged for re-decision).

        Returns 1 if the proposal was bounced to ``rebase_required``, else 0."""
        event = event_log.row_to_event(row)
        proposal_id = row.origin_proposal_id
        assert proposal_id is not None
        try:
            results = self._backend.commit_events([event], cas=True)
        except RebaseRequired:
            # The agent's value would clobber a committed change → it commits
            # NOTHING. Bounce the proposal and take the write out of the outbox;
            # the re-fold drops its optimistic effect (the intervening commit
            # stands), and the local row reverts to the pre-proposal state until
            # the next pull brings the team's value in.
            self._bounce_proposal(session, event, proposal_id)
            return 1
        for result in results:
            self._mark_committed_by_id(session, result.event_id, result.revision)
            if policy == PROPOSAL_POLICY_STRICT_REBASE and result.is_stale:
                # strict: the write auto-merged (different field) but raced a
                # change — flag the proposal for re-confirmation. The value stays
                # applied; it never conflicted.
                self._bounce_proposal(session, event, proposal_id, reverted=False)
                return 1
        return 0

    def _bounce_proposal(
        self, session: Session, event: Event, proposal_id: str, *, reverted: bool = True
    ) -> None:
        """Flip a proposal to ``rebase_required`` and (when ``reverted``) take its
        rejected ticket write out of the outbox + fold (MOD-26 §B3).

        The flip is a new synced ``agent_proposals`` event superseding the
        approved flip; the proposal's ``diff`` is preserved so the human re-applies
        it against current reality. Audited as a distinct decision."""
        if reverted:
            local = event_log.event_row(session, event.actor_id, event.actor_seq)
            if local is not None:
                local.sync_state = event_log.SYNC_STATE_REBASE_REQUIRED
                session.add(local)
                session.flush()
                refold_entity(session, event.collection, event.entity_id)
        flip = self._signed_event(
            "agent_proposals",
            proposal_id,
            {"status": "rebase_required"},
            event_log.entity_base_rev(session, "agent_proposals", proposal_id),
            event_log.next_actor_seq(session, self._actor_id),
        )
        for committed_flip in self._backend.commit_events([flip]):
            if event_log.event_by_id(session, flip.event_id) is None:
                event_log.insert_event(session, flip, committed_rev=committed_flip.revision)
        refold_entity(session, "agent_proposals", proposal_id)
        proposal = session.get(AgentProposal, proposal_id)
        audit.write(
            session,
            actor_id=self._actor_id,
            action="proposal.rebase_required",
            source="sync",
            object_ref=f"agent_proposals/{proposal_id}",
            after={
                "ticket_id": proposal.ticket_id if proposal is not None else None,
                "collection": event.collection,
                "entity_id": event.entity_id,
                "fields": sorted(event.payload),
                "applied": not reverted,
            },
        )

    def _mint_conflict_records(self, session: Session, result: CommitResult) -> int:
        """Mint a signed ``conflict_record`` per field-conflict the RPC reported
        (MOD-26 §B4). The deterministic id is hashed client-side from the raw
        tuple (no plpgsql hash); the record commits synchronously through the
        atomic RPC (authoritative_tx — never the optimistic outbox) and folds
        locally via the dedicated ingest. Concurrent mints on other replicas
        collapse to one row (insert-once on the deterministic id)."""
        if self._workspace_id is None:
            return 0  # a bare sync loop has no workspace scope to mint into
        contended = event_log.event_by_id(session, result.event_id)
        if contended is None:
            return 0
        minted = 0
        for fc in result.conflicts:
            revisions = sorted({result.revision, fc.contending_revision})
            cr_id = conflict_record_id(contended.entity_id, fc.field, revisions)
            payload = {
                "workspace_id": self._workspace_id,
                "collection": contended.collection,
                "entity_id": contended.entity_id,
                "field": fc.field,
                "contending_revisions": revisions,
                "candidate_values": {"keep_a": fc.head_value, "keep_b": fc.incoming_value},
                "base_rev": result.base_rev or 0,
                # The committed head AFTER this contending write — the revision a
                # resolution CASes against (rebase_required if it moves, B4).
                "head_rev": result.revision,
                "actor": contended.actor_id,
                "status": "open",
            }
            cr_event = Event(
                event_id=new_ulid(),
                collection="conflict_records",
                entity_id=cr_id,
                actor_id=self._actor_id,
                actor_seq=event_log.next_actor_seq(session, self._actor_id),
                op="patch",
                base_rev=None,
                policy_ref=self._signer.policy_ref if self._signer is not None else None,
                payload=payload,
                sig=None,
            )
            if self._signer is not None:
                cr_event = sign(cr_event, self._signer.private_key)
            for committed_cr in self._backend.commit_events([cr_event]):
                if event_log.event_by_id(session, cr_event.event_id) is None:
                    event_log.insert_event(session, cr_event, committed_rev=committed_cr.revision)
                refold_entity(session, "conflict_records", cr_id)
            minted += 1
        return minted

    # ------------------------------------------------------------- resolve

    def resolve_conflict(
        self,
        conflict_id: str,
        choice: str,
        *,
        new_value: object = None,
        resolved_by: str | None = None,
        now: datetime | None = None,
    ) -> ResolveResult:
        """Resolve a ``conflict_record`` (MOD-26 §B4).

        Emits a superseding field write (``base_rev = the record's head_rev``)
        AND flips the record to ``resolved``, committed **together** as a single
        compare-and-swap (``cas=True``) call to the atomic RPC. If the field head
        moved past ``head_rev`` the RPC commits **nothing** and raises
        ``RebaseRequired`` — so the resolution value never lands against a live
        newer contender (the resolver-vs-writer hole) and the record stays open
        and re-surfaces for the human to re-decide. ``status`` is sticky-resolved
        in the fold. ``choice ∈ {keep-A, keep-B, new-value}``. Resolution is a
        ``tickets.write``; for agents it is propose-first, enforced at the API edge.
        """
        if choice not in ("keep-A", "keep-B", "new-value"):
            raise ValueError(f"unknown resolve choice {choice!r}")
        resolved_by = resolved_by or self._actor_id
        with Session(self._db) as session:
            record = session.get(ConflictRecord, conflict_id)
            if record is None:
                raise KeyError(f"no conflict_record {conflict_id!r}")
            if record.status == "resolved":
                return ResolveResult(conflict_id, resolved=True, rebase_required=False)

            value = self._resolved_value(record, choice, new_value)
            stamp = (now or datetime.now(UTC)).isoformat()
            # The superseding write and the status flip are two events committed
            # together — assign sequential actor_seqs (neither is inserted yet, so
            # next_actor_seq alone would hand both the same number).
            seq = event_log.next_actor_seq(session, self._actor_id)
            field_event = self._signed_event(
                record.collection, record.entity_id, {record.field: value}, record.head_rev, seq
            )
            flip_event = self._signed_event(
                "conflict_records",
                conflict_id,
                {
                    "status": "resolved",
                    "resolved_by": resolved_by,
                    "resolved_choice": choice,
                    "resolved_at": stamp,
                },
                None,
                seq + 1,
            )
            try:
                committed = self._backend.commit_events([field_event, flip_event], cas=True)
            except RebaseRequired:
                # The contended field moved since the record was minted — the CAS
                # committed nothing. The record stays open and re-surfaces; the
                # human re-decides against the live contender. No value landed, so
                # there is no half-applied state and no partition window.
                return ResolveResult(conflict_id, resolved=False, rebase_required=True)
            by_id = {r.event_id: r for r in committed}
            for event in (field_event, flip_event):
                result = by_id.get(event.event_id)
                if result is None:
                    continue
                if event_log.event_by_id(session, event.event_id) is None:
                    event_log.insert_event(session, event, committed_rev=result.revision)
                refold_entity(session, event.collection, event.entity_id)
            # A conflict resolution is one of the §11 audited surfaces (DoD;
            # architecture fact: "resolved by a human, audited as a distinct
            # actor"). The superseding write is itself a new event, but this row
            # names the resolution decision explicitly, attributed to the
            # resolver, so the trail shows who chose and what.
            audit.write(
                session,
                actor_id=resolved_by,
                action="conflict.resolved",
                source="app",
                object_ref=f"conflict_records/{conflict_id}",
                after={
                    "collection": record.collection,
                    "entity_id": record.entity_id,
                    "field": record.field,
                    "choice": choice,
                    "resolved_value": value,
                },
            )
            session.commit()
        return ResolveResult(conflict_id, resolved=True, rebase_required=False)

    @staticmethod
    def _resolved_value(record: ConflictRecord, choice: str, new_value: object) -> object:
        if choice == "keep-A":
            return record.candidate_values.get("keep_a")
        if choice == "keep-B":
            return record.candidate_values.get("keep_b")
        return new_value

    def _signed_event(
        self,
        collection: str,
        entity_id: str,
        payload: dict[str, object],
        base_rev: int | None,
        actor_seq: int,
    ) -> Event:
        """Build (and, when a signer is present, sign) one event for a synchronous
        authoritative write — the resolution's superseding write + status flip."""
        event = Event(
            event_id=new_ulid(),
            collection=collection,
            entity_id=entity_id,
            actor_id=self._actor_id,
            actor_seq=actor_seq,
            op="patch",
            base_rev=base_rev,
            policy_ref=self._signer.policy_ref if self._signer is not None else None,
            payload=dict(payload),
        )
        return sign(event, self._signer.private_key) if self._signer is not None else event

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

    def _mark_committed_by_id(self, session: Session, event_id: str, revision: int) -> None:
        """Mark a row committed from a ``CommitResult`` (keyed by event_id)."""
        row = event_log.event_by_id(session, event_id)
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
