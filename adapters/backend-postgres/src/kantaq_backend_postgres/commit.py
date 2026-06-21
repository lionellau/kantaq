"""The atomic commit (E25-T1 / MOD-28) — the Python twin of ``events.sql``.

This is the self-hosted backend's commit path. It does, in one Postgres
transaction, exactly what the Supabase ``public.events`` plpgsql RPC does
(``supabase/rpc/events.sql``) — but by **reusing the shared validator core**,
not re-deriving it (D-30, the module's main constraint):

- **grant + signature validation** is ``kantaq_sync_engine.verify.verify_event``
  — the same function the local runtime's ``VerifyingBackend`` runs. The
  caller injects a ``verify`` closure that builds a fresh ``VerifyContext`` from
  the server's trust tables (``verification_roots`` / ``local_grant_index``), so
  the eight grant checks (held → live issuer → not revoked → window → subject →
  resource → verb) are byte-for-byte the ones the plpgsql mirrors. Because the
  server is Python, ``verify_event`` *also* verifies the Ed25519 bytes — the one
  check the plpgsql cannot do (D-09); the self-hosted server is therefore a
  superset of the Supabase server-side posture, never a subset.
- **the per-field merge decision** is ``kantaq_sync_engine.merge.detect_merge``
  — the single §8.1 reference the plpgsql ``event_conflicts`` mirrors, pinned
  equal by the golden ``conflict_vectors.json``. The self-hosted server runs the
  reference itself, so "one decision, one truth" holds by construction, not by a
  second hand-kept copy.

Atomicity matches the RPC: pass 1 validates **every** event before any commits
(a single failure raises ``EventRejected`` and the transaction rolls back —
nothing lands); the per-workspace advisory xact lock serialises commit-order
assignment so revision N is fully committed before N+1 is assigned (closing the
v0.1 commit-visibility window); ``cas=True`` refuses the whole call with
``RebaseRequired`` if any write would contend with the committed head.

The function operates on a live ``Connection`` inside the caller's transaction;
the caller owns commit/rollback (the in-process ``PostgresSyncBackend`` and the
HTTP server both wrap it the same way).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from kantaq_backend_postgres.schema import sync_events
from kantaq_db.meta import COLLECTION_META
from kantaq_protocol import Event
from kantaq_sync_engine.events import CommitResult, CommittedEvent, FieldConflict, RebaseRequired
from kantaq_sync_engine.merge import detect_merge
from kantaq_sync_engine.verify import EventRejected, EventVerification, verify_event

__all__ = ["EventRejected", "commit_events", "to_commit_result", "verify_event"]

Verifier = Callable[[Event], EventVerification]


def _advisory_lock(conn: Connection, workspace_id: str) -> None:
    """Hold the per-workspace xact lock so commit order is assigned serially.

    Mirrors ``pg_advisory_xact_lock(hashtext('kantaq.sync_events:' || ws))`` in
    events.sql — the same lock key, so the self-hosted server and a (hypothetical
    co-located) Supabase RPC would contend on the same lock rather than racing.
    Auto-released at COMMIT/ROLLBACK.
    """
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('kantaq.sync_events:' || :ws))"),
        {"ws": workspace_id},
    )


def _entity_head(conn: Connection, workspace_id: str, collection: str, entity_id: str) -> int:
    """The committed head revision for one entity (LWW by commit order, D-05)."""
    head = conn.execute(
        select(func.coalesce(func.max(sync_events.c.revision), 0)).where(
            sync_events.c.workspace_id == workspace_id,
            sync_events.c.collection == collection,
            sync_events.c.entity_id == entity_id,
        )
    ).scalar_one()
    return int(head)


def _committed_prefix(
    conn: Connection, workspace_id: str, collection: str, entity_id: str
) -> list[CommittedEvent]:
    """The entity's full committed history, in commit order, as ``CommittedEvent``.

    This is the gapless prefix ``detect_merge`` requires (it is read under the
    workspace advisory lock, in the same transaction that will assign the
    incoming revision, so no concurrent write can open a gap)."""
    rows = conn.execute(
        select(
            sync_events.c.revision,
            sync_events.c.event_id,
            sync_events.c.collection,
            sync_events.c.entity_id,
            sync_events.c.actor_id,
            sync_events.c.actor_seq,
            sync_events.c.op,
            sync_events.c.base_rev,
            sync_events.c.policy_ref,
            sync_events.c.payload,
            sync_events.c.sig,
        )
        .where(
            sync_events.c.workspace_id == workspace_id,
            sync_events.c.collection == collection,
            sync_events.c.entity_id == entity_id,
        )
        .order_by(sync_events.c.revision)
    ).all()
    return [
        CommittedEvent(
            revision=int(r.revision),
            event=Event(
                event_id=r.event_id,
                collection=r.collection,
                entity_id=r.entity_id,
                actor_id=r.actor_id,
                actor_seq=int(r.actor_seq),
                op=r.op,
                base_rev=int(r.base_rev) if r.base_rev is not None else None,
                policy_ref=r.policy_ref,
                payload=dict(r.payload or {}),
                sig=r.sig,
            ),
        )
        for r in rows
    ]


def _conflicts_for(
    conn: Connection, workspace_id: str, event: Event, head: int
) -> list[dict[str, Any]]:
    """The per-field conflicts an incoming ``patch`` contends (E05-T2 / §B4).

    Returned as the **raw conflict dicts** the plpgsql RPC emits
    (``{field, contending_revision, head_value, incoming_value}``) so the
    self-hosted result is byte-identical to the Supabase RPC's ``conflicts[]``.

    The gate mirrors events.sql exactly: only a ``patch`` on an ``lww``
    collection whose head has moved past its base can conflict (append-only logs
    and authoritative_tx tables never mint a conflict_record). The decision is
    ``detect_merge`` — the shared reference — so the tuple is identical to what
    the Supabase RPC returns and what the client preview computes.
    """
    base_eff = event.base_rev if event.base_rev is not None else 0
    meta = COLLECTION_META.get(event.collection)
    policy = meta.merge_policy if meta is not None else None
    if event.op != "patch" or policy != "lww" or head <= base_eff:
        return []
    prefix = _committed_prefix(conn, workspace_id, event.collection, event.entity_id)
    # The incoming revision is a placeholder: a conflict carries only the raw
    # contender tuple (field, contending_revision, both values); the incoming
    # revision feeds only conflict_record_id, which is hashed client-side and not
    # part of this result (no cross-language id drift — D-09 / merge.py).
    incoming = CommittedEvent(revision=head + 1, event=event)
    outcome = detect_merge(prefix, incoming)
    return [
        {
            "field": d.field,
            "contending_revision": int(d.contending_revision)
            if d.contending_revision is not None
            else None,
            "head_value": d.head_value,
            "incoming_value": d.incoming_value,
        }
        for d in outcome.conflicts
    ]


def commit_events(
    conn: Connection,
    workspace_id: str,
    events: Sequence[Event],
    *,
    verify: Verifier,
    cas: bool = False,
) -> list[dict[str, Any]]:
    """Atomically commit ``events`` for one workspace; return each outcome.

    Returns the **raw result rows** the plpgsql RPC returns — one dict per
    submitted event with the same keys (``event_id``, ``status``, ``revision``,
    ``base_rev``, ``head_rev``, ``stale_base_rev``, ``conflicts``) and the same
    null conventions (a duplicate carries ``base_rev``/``head_rev`` null) — so
    the self-hosted ``/rpc/events`` response is byte-identical to Supabase's.
    Use ``to_commit_result`` for the typed ``BackendPort`` view.

    ``verify`` is the injected per-event verdict (the server builds a closure
    over a fresh ``VerifyContext``; pre-cutover seeding passes a permissive one).
    The two-pass structure mirrors events.sql: validate all, lock, then commit in
    submission order. ``EventRejected`` (pass 1) and ``RebaseRequired`` (pass 2,
    ``cas``) both leave the transaction for the caller to roll back — nothing
    partially commits.
    """
    batch = list(events)

    # ---- pass 1: validate EVERY event before committing ANY (atomic reject).
    for event in batch:
        verdict = verify(event)
        if not verdict.ok:
            raise EventRejected(verdict, event)

    if not batch:
        return []

    _advisory_lock(conn, workspace_id)

    # ---- pass 2: commit in submission order under the held lock.
    results: list[dict[str, Any]] = []
    for event in batch:
        head = _entity_head(conn, workspace_id, event.collection, event.entity_id)
        base = event.base_rev
        stale = base if (base is not None and base < head) else None
        conflicts = _conflicts_for(conn, workspace_id, event, head)

        if cas and conflicts:
            # Compare-and-swap refusal: roll the whole call back so nothing lands
            # (the caller maps this to a re-surfaced conflict / rebase_required).
            raise RebaseRequired(event, tuple(FieldConflict(**c) for c in conflicts))

        inserted = conn.execute(
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
                workspace_id=workspace_id,
            )
            .on_conflict_do_nothing(index_elements=["actor_id", "actor_seq"])
            .returning(sync_events.c.revision)
        ).first()

        if inserted is None:
            # Dedup floor hit (idempotent re-push): the prior commit's revision.
            # This event did not commit now, so its merge metadata is not
            # meaningful — report it null, exactly as events.sql does.
            existing = conn.execute(
                select(sync_events.c.revision).where(
                    sync_events.c.actor_id == event.actor_id,
                    sync_events.c.actor_seq == event.actor_seq,
                )
            ).scalar_one()
            results.append(
                {
                    "event_id": event.event_id,
                    "status": "duplicate",
                    "revision": int(existing),
                    "base_rev": None,
                    "head_rev": None,
                    "stale_base_rev": None,
                    "conflicts": [],
                }
            )
        else:
            results.append(
                {
                    "event_id": event.event_id,
                    "status": "committed",
                    "revision": int(inserted[0]),
                    "base_rev": base,
                    "head_rev": head,
                    "stale_base_rev": stale,
                    "conflicts": conflicts,
                }
            )
    return results


def to_commit_result(row: dict[str, Any]) -> CommitResult:
    """Map a raw RPC result row to the typed ``CommitResult`` (BackendPort view).

    Mirrors ``SupabaseSyncBackend._row_to_commit_result`` so the two backends'
    typed results are identical, including null-safe ``head_rev`` (a duplicate
    has ``head_rev`` null → ``head_rev=0``). The Supabase adapter adopted the
    same guard at E25-T4 (DEBT-40 closed), so the two map a duplicate row
    identically — pinned by ``test_debt40_both_adapters_map_a_duplicate``.
    """
    stale = row.get("stale_base_rev")
    base = row.get("base_rev")
    head = row.get("head_rev")
    return CommitResult(
        event_id=row["event_id"],
        status=row["status"],
        revision=int(row["revision"]),
        base_rev=int(base) if base is not None else None,
        head_rev=int(head) if head is not None else 0,
        stale_base_rev=int(stale) if stale is not None else None,
        conflicts=tuple(
            FieldConflict(
                field=c["field"],
                contending_revision=int(c["contending_revision"]),
                head_value=c.get("head_value"),
                incoming_value=c.get("incoming_value"),
            )
            for c in (row.get("conflicts") or ())
        ),
    )
