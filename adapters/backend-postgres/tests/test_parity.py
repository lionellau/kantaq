"""E25-T1: the self-hosted backend's merge == the shared §8.1 reference.

One decision, one truth (MOD-28's main constraint, D-30): the SAME golden
``conflict_vectors.json`` that pins ``detect_merge`` (Python) and the Supabase
plpgsql RPC (``test_conflict_rpc_parity``) is replayed through
``PostgresSyncBackend.commit_events`` on a real EphemeralPostgres, and the
``conflicts[]`` the self-hosted commit returns must equal **both**:

- the golden ``expected_conflicts`` (the ground truth — a *non-circular* check),
  and
- ``detect_merge`` over the actually-committed prefix (the wiring is correct).

Since ``test_conflict_rpc_parity`` already pins the Supabase RPC == detect_merge,
this pins the self-hosted backend == detect_merge, so by transitivity the two
backends agree on every conflict — "one validator core, two backends" proven on
real Postgres, not asserted in a comment.

Vectors carry vector-local revisions; the database assigns its own. We commit
each event, capture the db revision, and remap every ``base_rev`` and expected
``contending_revision`` through that map so the two sides compare on identical
revisions (the discipline ``test_conflict_rpc_parity`` uses for the RPC).
"""

from __future__ import annotations

import itertools
import json
from typing import Any

from kantaq_backend_postgres import PostgresSyncBackend
from kantaq_protocol import Event
from kantaq_sync_engine import CommittedEvent
from kantaq_sync_engine.merge import detect_merge
from kantaq_test_harness.vectors import ConflictVector, load_conflict_vectors

_seq = itertools.count(1)
_eid = itertools.count(1)


def _norm(conflicts: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
    """A comparable set: field, contender revision, both candidate values."""
    return {
        (
            c["field"],
            c["contending_revision"],
            json.dumps(c["head_value"], sort_keys=True),
            json.dumps(c["incoming_value"], sort_keys=True),
        )
        for c in conflicts
    }


def _commit_remapped(
    backend: PostgresSyncBackend,
    *,
    entity_id: str,
    src: CommittedEvent,
    remap: dict[int, int],
) -> tuple[CommittedEvent, Any]:
    """Commit one vector event (pre-cutover/unsigned), remapping its base_rev to
    the already-committed db revision; record vector-rev → db-rev and return the
    committed event carrying the assigned revision plus the raw CommitResult."""
    base = remap.get(src.event.base_rev) if src.event.base_rev is not None else None
    event = Event(
        event_id=f"e{next(_eid):025d}",
        collection=src.event.collection,
        entity_id=entity_id,
        actor_id=src.event.actor_id,
        actor_seq=next(_seq),
        op=src.event.op,
        base_rev=base,
        policy_ref=None,
        payload=dict(src.event.payload),
        sig=None,
    )
    result = backend.commit_events([event], require_signature=False)[0]
    remap[src.revision] = result.revision
    return CommittedEvent(revision=result.revision, event=event), result


def _replay(
    backend: PostgresSyncBackend, vector: ConflictVector, entity_id: str
) -> tuple[Any, list[CommittedEvent], CommittedEvent, dict[int, int]]:
    """Replay a vector's prefix then its incoming through the backend."""
    remap: dict[int, int] = {}
    committed: list[CommittedEvent] = []
    for src in vector.committed_prefix:
        ce, _ = _commit_remapped(backend, entity_id=entity_id, src=src, remap=remap)
        committed.append(ce)
    inc, result = _commit_remapped(backend, entity_id=entity_id, src=vector.incoming, remap=remap)
    return result, committed, inc, remap


def test_self_hosted_merge_matches_golden_and_detect_merge(pg_backend: PostgresSyncBackend) -> None:
    vectors = load_conflict_vectors()
    assert vectors, "no conflict vectors loaded"
    for i, vector in enumerate(vectors):
        entity_id = f"ent{i:023d}"
        result, committed, inc, remap = _replay(pg_backend, vector, entity_id)

        # what the self-hosted commit reported
        mine = _norm(
            [
                {
                    "field": c.field,
                    "contending_revision": c.contending_revision,
                    "head_value": c.head_value,
                    "incoming_value": c.incoming_value,
                }
                for c in result.conflicts
            ]
        )

        # the golden ground truth, remapped to the db's assigned revisions
        golden = _norm(
            [
                {
                    "field": c["field"],
                    "contending_revision": remap[c["contending_revision"]],
                    "head_value": c["head_value"],
                    "incoming_value": c["incoming_value"],
                }
                for c in vector.expected_conflicts
            ]
        )

        # detect_merge over the actually-committed prefix (the shared reference)
        outcome = detect_merge(committed, inc)
        via_detect = _norm(
            [
                {
                    "field": d.field,
                    "contending_revision": d.contending_revision,
                    "head_value": d.head_value,
                    "incoming_value": d.incoming_value,
                }
                for d in outcome.conflicts
            ]
        )

        assert mine == golden, f"{vector.name}: self-hosted {mine} != golden {golden}"
        assert mine == via_detect, f"{vector.name}: self-hosted {mine} != detect_merge {via_detect}"
        assert outcome.entity_verdict == vector.expected_verdict, (
            f"{vector.name}: verdict {outcome.entity_verdict} != {vector.expected_verdict}"
        )


def test_stale_base_rev_reported_when_base_behind_head(pg_backend: PostgresSyncBackend) -> None:
    """A write whose base predates the committed head reports stale_base_rev (the
    metadata a committing client mints a conflict_record from) — matching the RPC."""
    base_ev = Event(
        event_id=f"e{next(_eid):025d}",
        collection="tickets",
        entity_id="tkt_stale00000000000000001",
        actor_id="mbr_aa0000000000000000000",
        actor_seq=next(_seq),
        op="patch",
        base_rev=None,
        policy_ref=None,
        payload={"title": "v1"},
        sig=None,
    )
    first = pg_backend.commit_events([base_ev], require_signature=False)[0]
    assert first.stale_base_rev is None  # genesis is never stale

    # a second writer moves the head
    pg_backend.commit_events(
        [
            Event(
                event_id=f"e{next(_eid):025d}",
                collection="tickets",
                entity_id="tkt_stale00000000000000001",
                actor_id="mbr_bb0000000000000000000",
                actor_seq=next(_seq),
                op="patch",
                base_rev=first.revision,
                policy_ref=None,
                payload={"title": "v2"},
                sig=None,
            )
        ],
        require_signature=False,
    )

    # a third write still based on the genesis revision is stale
    stale = pg_backend.commit_events(
        [
            Event(
                event_id=f"e{next(_eid):025d}",
                collection="tickets",
                entity_id="tkt_stale00000000000000001",
                actor_id="mbr_aa0000000000000000000",
                actor_seq=next(_seq),
                op="patch",
                base_rev=first.revision,
                policy_ref=None,
                payload={"assignee": "bob"},
                sig=None,
            )
        ],
        require_signature=False,
    )[0]
    assert stale.is_stale
    assert stale.stale_base_rev == first.revision
