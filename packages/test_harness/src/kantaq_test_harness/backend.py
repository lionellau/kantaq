"""FakeBackend — an in-memory model of the shared sync backend.

It does the backend's minimum (architecture §2): validate, store, assign a
commit order. v0.0.5 semantics: dedup by ``(actor_id, actor_seq)`` so re-push is
idempotent (NFR-E04-2), and fold by commit order = last-writer-wins (D-05). This
is the contract the real sync engine (E04) and backend (E24) tests target.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# The canonical protocol types (MOD-04): FakeBackend satisfies the engine's
# BackendPort nominally, not just structurally — and folds with the engine's
# own fold_events, so the contract the real adapters (MOD-05/28) implement is
# pinned to one shape.
from kantaq_db.meta import COLLECTION_META
from kantaq_db.schema_version import EXPECTED_SCHEMA_VERSION
from kantaq_sync_engine.events import (
    SYNC_VERSION,
    BackendUnavailable,
    CommitResult,
    CommittedEvent,
    FieldConflict,
    RebaseRequired,
    SessionInit,
    fold_events,
)
from kantaq_sync_engine.merge import detect_merge
from kantaq_test_harness.models import Event

__all__ = ["CommitResult", "CommittedEvent", "Event", "FakeBackend", "PartitionLink"]


class FakeBackend:
    def __init__(self) -> None:
        self._log: list[CommittedEvent] = []
        self._seen: set[tuple[str, int]] = set()
        self._revision = 0
        # Partition primitive (MOD-26 §B1 / RISK-04): flip ``offline`` and every
        # push/pull raises ``BackendUnavailable``, modelling a dropped connection.
        # The log is untouched, so flipping it back resumes exactly where it left
        # off — what the offline-aware flush loop and the partition proofs need.
        self.offline = False
        # §B7 handshake: the versions this backend advertises. Tests bump these to
        # model a peer the engine should refuse (out-of-range) or tolerate (±1).
        self.sync_version = SYNC_VERSION
        self.schema_version = EXPECTED_SCHEMA_VERSION

    @property
    def revision(self) -> int:
        return self._revision

    def session_init(self, *, sync_version: int, schema_version: int) -> SessionInit:
        """Mirror the RPC's handshake: record the client's advertised versions
        (a no-op for the fake) and return our own (MOD-26 §B7)."""
        del sync_version, schema_version  # the real RPC logs these; the fake echoes
        return SessionInit(self.sync_version, self.schema_version)

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Append new events, assigning a monotonic revision. Duplicates (same
        actor_id+actor_seq) are silently dropped so retries cannot duplicate."""
        if self.offline:
            raise BackendUnavailable("fake backend is partitioned (offline)")
        committed: list[CommittedEvent] = []
        for event in events:
            key = (event.actor_id, event.actor_seq)
            if key in self._seen:
                continue
            self._seen.add(key)
            self._revision += 1
            entry = CommittedEvent(revision=self._revision, event=event)
            self._log.append(entry)
            committed.append(entry)
        return committed

    def commit_events(
        self, events: Iterable[Event], *, require_signature: bool = True, cas: bool = False
    ) -> list[CommitResult]:
        """The v0.2 atomic-RPC commit path (E24-T6): mirrors the real RPC's
        contract — dedup by (actor_id, actor_seq), LWW by commit order, and a
        per-event ``stale_base_rev`` when ``base_rev`` is older than the
        committed head for the entity. Shares the log with ``push`` so
        pull/snapshot/len see both paths. The fake does not enforce
        ``require_signature`` (byte-verification is the VerifyingBackend's job,
        mirroring D-09).

        ``cas`` (MOD-26 §B3/B4): a compare-and-swap commit — if any write in the
        call WOULD contend with the entity's committed field head, NOTHING is
        committed and ``RebaseRequired`` is raised (atomic), mirroring the
        ``p_cas`` branch of ``events.sql``. Used for resolutions + approved agent
        proposals (a stale write that must not silently land)."""
        if self.offline:
            raise BackendUnavailable("fake backend is partitioned (offline)")
        batch = list(events)
        if cas:
            # Pass 1: refuse the whole call if any committed write would contend,
            # against the head BEFORE this call (no event in the batch committed
            # yet) — the atomic CAS the RPC's p_cas branch enforces.
            for event in batch:
                next_rev = self._revision + 1
                conflicts = self._detect_conflicts(event, next_rev)
                if conflicts:
                    raise RebaseRequired(event, conflicts)
        results: list[CommitResult] = []
        for event in batch:
            head = max(
                (
                    entry.revision
                    for entry in self._log
                    if entry.event.collection == event.collection
                    and entry.event.entity_id == event.entity_id
                ),
                default=0,
            )
            key = (event.actor_id, event.actor_seq)
            if key in self._seen:
                prior = next(
                    entry
                    for entry in self._log
                    if (entry.event.actor_id, entry.event.actor_seq) == key
                )
                results.append(
                    CommitResult(
                        event_id=event.event_id,
                        status="duplicate",
                        revision=prior.revision,
                        base_rev=None,
                        head_rev=head,
                        stale_base_rev=None,
                    )
                )
                continue
            self._seen.add(key)
            self._revision += 1
            self._log.append(CommittedEvent(revision=self._revision, event=event))
            stale = event.base_rev if event.base_rev is not None and event.base_rev < head else None
            results.append(
                CommitResult(
                    event_id=event.event_id,
                    status="committed",
                    revision=self._revision,
                    base_rev=event.base_rev,
                    head_rev=head,
                    stale_base_rev=stale,
                    conflicts=self._detect_conflicts(event, self._revision),
                )
            )
        return results

    def _detect_conflicts(self, event: Event, revision: int) -> tuple[FieldConflict, ...]:
        """Mirror the RPC's per-field conflict detection for the optimistic-domain
        (lww) collections — authoritative_tx / append_only never mint a record.
        Runs the shared ``detect_merge`` over the entity's committed prefix, so the
        fake and the real plpgsql RPC agree on the same golden vectors (E05-T2)."""
        meta = COLLECTION_META.get(event.collection)
        if meta is None or meta.merge_policy != "lww":
            return ()
        prefix = [
            entry
            for entry in self._log
            if entry.event.collection == event.collection
            and entry.event.entity_id == event.entity_id
            and entry.revision < revision
        ]
        outcome = detect_merge(prefix, CommittedEvent(revision=revision, event=event))
        return tuple(
            FieldConflict(
                field=d.field,
                contending_revision=d.contending_revision,
                head_value=d.head_value,
                incoming_value=d.incoming_value,
            )
            for d in outcome.conflicts
            if d.contending_revision is not None
        )

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        if self.offline:
            raise BackendUnavailable("fake backend is partitioned (offline)")
        return [
            entry
            for entry in self._log
            if entry.revision > since
            and (collection is None or entry.event.collection == collection)
        ]

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """Fold the log into current entity state (LWW by commit order)."""
        return fold_events(
            entry.event for entry in self._log if entry.event.collection == collection
        )

    def __len__(self) -> int:
        return len(self._log)


class PartitionLink:
    """A per-replica view of a shared backend that can be partitioned alone.

    ``FakeBackend.offline`` partitions *every* replica sharing the backend; wrap
    each replica's backend in a ``PartitionLink`` to drop just that one replica's
    link — the partition simulator MOD-26 §RISK-04 wants for N-way heal proofs.
    It is a ``BackendPort``: it delegates while ``online`` and raises
    ``BackendUnavailable`` when not, so the shared log stays intact and a heal
    resumes exactly where the partition began.
    """

    def __init__(self, backend: FakeBackend, *, online: bool = True) -> None:
        self._backend = backend
        self.online = online

    def _require_link(self) -> None:
        if not self.online:
            raise BackendUnavailable("replica is partitioned from the backend")

    def session_init(self, *, sync_version: int, schema_version: int) -> SessionInit:
        self._require_link()
        return self._backend.session_init(sync_version=sync_version, schema_version=schema_version)

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        self._require_link()
        return self._backend.push(events)

    def commit_events(
        self, events: Iterable[Event], *, require_signature: bool = True, cas: bool = False
    ) -> list[CommitResult]:
        self._require_link()
        return self._backend.commit_events(events, require_signature=require_signature, cas=cas)

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        self._require_link()
        return self._backend.pull(collection, since)

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        self._require_link()
        return self._backend.snapshot(collection)
