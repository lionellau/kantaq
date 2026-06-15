"""E05-T1 DEBT-25 cutover: the engine commits through the atomic-RPC path.

flush_outbox now drains via ``commit_events`` (not the raw ``push``), so the
commit path is the advisory-locked RPC for every write. These pin the
``CommitResult`` contract the engine maps — dedup-idempotency and the
``stale_base_rev`` signal E05-T2 turns into a conflict_record.
"""

from __future__ import annotations

from kantaq_sync_engine.events import Event
from kantaq_test_harness.backend import FakeBackend


def _event(seq: int, base_rev: int | None, *, entity: str = "tkt_000000000000000000001") -> Event:
    return Event(
        event_id=f"evt_{seq:022d}",
        collection="tickets",
        entity_id=entity,
        actor_id="mbr_a00000000000000000000",
        actor_seq=seq,
        op="patch",
        base_rev=base_rev,
        payload={"status": "doing"},
    )


def test_commit_events_reports_stale_when_base_rev_is_behind_head() -> None:
    backend = FakeBackend()

    first = backend.commit_events([_event(1, None)])
    assert first[0].status == "committed"
    assert first[0].revision == 1
    assert first[0].stale_base_rev is None  # genesis: never stale

    # A second write on the same entity whose base_rev (0) predates the committed
    # head (1): committed LWW-by-order, but reported stale so E05-T2 can mint a
    # conflict_record.
    second = backend.commit_events([_event(2, 0)])
    assert second[0].status == "committed"
    assert second[0].head_rev == 1
    assert second[0].stale_base_rev == 0
    assert second[0].is_stale


def test_commit_events_is_idempotent_on_redelivery() -> None:
    backend = FakeBackend()
    backend.commit_events([_event(1, None)])

    again = backend.commit_events([_event(1, None)])  # same (actor_id, actor_seq)
    assert again[0].status == "duplicate"
    assert again[0].revision == 1
    assert len(backend) == 1  # exactly-once at the commit floor
