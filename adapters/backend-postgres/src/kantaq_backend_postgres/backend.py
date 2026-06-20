"""``PostgresSyncBackend`` — the self-hosted ``BackendPort`` (E25-T1 / MOD-28).

The in-process backend over one self-hosted Postgres database, scoped to one
workspace — the structural twin of ``SupabaseSyncBackend`` but talking SQL to a
plain Postgres instead of PostgREST to Supabase. It implements the exact same
``kantaq_sync_engine.events.BackendPort`` (the contract MOD-30's FakeBackend
pins), so the sync engine, the convergence fixtures, and the parity suite drive
it unchanged.

The atomic commit (``commit_events``) delegates to ``commit.commit_events`` and
builds its ``VerifyContext`` from the **same trust readers the runtime uses** —
``verification_roots`` (device → key) and ``local_grant_index`` (grant id →
grant, plus the revoked set) over this server's own ``devices`` /
``capability_grants`` tables. So the eight grant checks are the shared
``verify_event``, not a re-derivation: one validator core, two backends (D-30).

The HTTP sync-server (``app.py``) uses this class as its data layer; the runtime
uses it (over HTTP, via ``SyncServerBackend``) when ``HUB_MODE=postgres``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_backend_postgres.commit import commit_events as _commit_events
from kantaq_backend_postgres.commit import to_commit_result
from kantaq_backend_postgres.schema import sync_acks, sync_events
from kantaq_core.identity import local_grant_index, verification_roots
from kantaq_db.schema_version import EXPECTED_SCHEMA_VERSION
from kantaq_sync_engine import VerifyContext
from kantaq_sync_engine.events import (
    SYNC_VERSION,
    CommitResult,
    CommittedEvent,
    Event,
    Op,
    SessionInit,
    fold_events,
)
from kantaq_sync_engine.verify import EventVerification, verify_event

PAGE_SIZE = 500


class PostgresSyncBackend:
    """The MOD-04 backend port over one self-hosted Postgres, one workspace.

    ``now`` is injectable (a FakeClock in tests) so the grant-window checks in
    ``verify_event`` are deterministic; it defaults to wall-clock unix seconds.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        workspace_id: str,
        now: Callable[[], int] | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._engine = engine
        self._workspace_id = workspace_id
        self._now = now or (lambda: int(datetime.now(UTC).timestamp()))
        self._page_size = page_size

    # --------------------------------------------------------------- the port

    def session_init(self, *, sync_version: int, schema_version: int) -> SessionInit:
        """Advertise the server's compile-time versions (§B7 / DEBT-09).

        The client advertises its versions; the self-hosted server returns its
        own, and the engine's ±1 skew check decides interop — the same handshake
        the Supabase backend performs, now answered by a real server endpoint
        rather than echoed client-side.
        """
        del sync_version, schema_version
        return SessionInit(SYNC_VERSION, EXPECTED_SCHEMA_VERSION)

    def _verify_context(self, require_signature: bool) -> VerifyContext:
        """Build the trust context from this server's own tables (shared readers)."""
        with Session(self._engine) as session:
            grants, revoked = local_grant_index(session)
            return VerifyContext(
                roots=verification_roots(session),
                grants=grants,
                now=self._now(),
                revoked_ids=revoked,
                require_signature=require_signature,
                workspace_id=self._workspace_id,
            )

    def verifier(self, *, require_signature: bool = True) -> Callable[[Event], EventVerification]:
        """The per-event verdict closure (the server reuses it for caller-binding)."""
        ctx = self._verify_context(require_signature)
        return lambda event: verify_event(event, ctx)

    def commit_events(
        self, events: Iterable[Event], *, require_signature: bool = True, cas: bool = False
    ) -> list[CommitResult]:
        """Commit through the atomic Python path (the events.sql twin).

        One transaction: validate every event (the shared ``verify_event``, here
        including the Ed25519 bytes — stronger than the plpgsql, D-09), then
        commit in order under the per-workspace advisory lock. ``EventRejected``
        (a failed check) and ``RebaseRequired`` (``cas`` contention) both roll the
        whole transaction back — nothing partially commits.
        """
        verify = self.verifier(require_signature=require_signature)
        with self._engine.begin() as conn:
            raw = _commit_events(conn, self._workspace_id, list(events), verify=verify, cas=cas)
        return [to_commit_result(r) for r in raw]

    def commit_events_raw(
        self,
        events: Iterable[Event],
        *,
        verify: Callable[[Event], EventVerification],
        cas: bool = False,
    ) -> list[dict[str, Any]]:
        """Commit with a caller-supplied verifier, returning raw RPC rows.

        The HTTP server uses this so it can wrap ``verify_event`` with its
        caller-binding (actor == the authenticated member) and return the
        byte-parity JSON directly.
        """
        with self._engine.begin() as conn:
            return _commit_events(conn, self._workspace_id, list(events), verify=verify, cas=cas)

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Raw transport (no verification) — convergence fixtures + pre-cutover.

        Mirrors ``SupabaseSyncBackend.push``: bulk insert with the (actor_id,
        actor_seq) dedup floor so a retry never double-commits. Verification, when
        required, is the ``VerifyingBackend`` wrapper's job on this path (D-05);
        the cutover routes real writes through ``commit_events`` instead.
        """
        committed: list[CommittedEvent] = []
        with self._engine.begin() as conn:
            for event in events:
                row = conn.execute(
                    pg_insert(sync_events)
                    .values(
                        event_id=event.event_id,
                        collection=event.collection,
                        entity_id=event.entity_id,
                        actor_id=event.actor_id,
                        actor_seq=event.actor_seq,
                        op=event.op,
                        base_rev=event.base_rev,
                        policy_ref=event.policy_ref,
                        payload=dict(event.payload),
                        sig=event.sig,
                        workspace_id=self._workspace_id,
                    )
                    .on_conflict_do_nothing(index_elements=["actor_id", "actor_seq"])
                    .returning(sync_events.c.revision)
                ).first()
                if row is not None:
                    committed.append(CommittedEvent(revision=int(row[0]), event=event))
        return committed

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        """Committed events with revision > ``since``, in commit order."""
        entries: list[CommittedEvent] = []
        cursor = since
        while True:
            stmt = (
                select(sync_events)
                .where(
                    sync_events.c.workspace_id == self._workspace_id,
                    sync_events.c.revision > cursor,
                )
                .order_by(sync_events.c.revision)
                .limit(self._page_size)
            )
            if collection is not None:
                stmt = stmt.where(sync_events.c.collection == collection)
            with self._engine.connect() as conn:
                page = [self._row_to_committed(r._mapping) for r in conn.execute(stmt).all()]
            entries.extend(page)
            if len(page) < self._page_size:
                return entries
            cursor = page[-1].revision

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """The backend's fold of a collection (LWW by commit order)."""
        return fold_events(entry.event for entry in self.pull(collection))

    # ------------------------------------------------- the ack watermark (E07-T4)

    def update_ack_watermark(
        self, *, member_id: str, replica_id: str, acked_rev: int, now: datetime | None = None
    ) -> None:
        """Report this replica's acked pull position (MOD-05 ack watermark).

        Upserts ``sync_acks`` keyed by (workspace, member, replica), so retention
        compaction can compute MIN(acked_rev) across live replicas and never prune
        a row a lagging replica still needs — the same contract as Supabase mode.
        """
        ts = now or datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(sync_acks)
                .values(
                    workspace_id=self._workspace_id,
                    member_id=member_id,
                    replica_id=replica_id,
                    acked_rev=acked_rev,
                    updated_at=ts,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "member_id", "replica_id"],
                    set_={"acked_rev": acked_rev, "updated_at": ts},
                )
            )

    def safe_watermark_rev(self, *, ttl_days: int = 30, now: datetime | None = None) -> int | None:
        """The lowest acked revision across replicas live within ``ttl_days``."""
        from datetime import timedelta

        cutoff = (now or datetime.now(UTC)) - timedelta(days=ttl_days)
        with self._engine.connect() as conn:
            value = conn.execute(
                select(func.min(sync_acks.c.acked_rev)).where(
                    sync_acks.c.workspace_id == self._workspace_id,
                    sync_acks.c.updated_at >= cutoff,
                )
            ).scalar()
        return int(value) if value is not None else None

    # --------------------------------------------------------------- plumbing

    @staticmethod
    def _row_to_committed(row: Any) -> CommittedEvent:
        op: Op = row["op"]
        return CommittedEvent(
            revision=int(row["revision"]),
            event=Event(
                event_id=row["event_id"],
                collection=row["collection"],
                entity_id=row["entity_id"],
                actor_id=row["actor_id"],
                actor_seq=int(row["actor_seq"]),
                op=op,
                base_rev=int(row["base_rev"]) if row["base_rev"] is not None else None,
                policy_ref=row["policy_ref"],
                payload=dict(row["payload"] or {}),
                sig=row["sig"],
            ),
        )
