"""FakeBackend: commit order, idempotent re-push, LWW fold, tombstones, cursors."""

from kantaq_test_harness import FakeBackend, build_event


def test_push_assigns_monotonic_revisions() -> None:
    backend = FakeBackend()
    committed = backend.push(
        [
            build_event(actor_id="a", actor_seq=1),
            build_event(actor_id="a", actor_seq=2),
        ]
    )
    assert [c.revision for c in committed] == [1, 2]
    assert backend.revision == 2


def test_repush_is_idempotent_dedup_by_actor_seq() -> None:
    backend = FakeBackend()
    event = build_event(actor_id="a", actor_seq=1)
    backend.push([event])
    second = backend.push([event])
    assert second == []
    assert len(backend) == 1


def test_snapshot_is_last_writer_wins_by_commit_order() -> None:
    backend = FakeBackend()
    backend.push(
        [
            build_event(
                collection="tickets",
                entity_id="t1",
                actor_id="a",
                actor_seq=1,
                payload={"status": "todo"},
            ),
            build_event(
                collection="tickets",
                entity_id="t1",
                actor_id="b",
                actor_seq=1,
                payload={"status": "done"},
            ),
        ]
    )
    assert backend.snapshot("tickets")["t1"]["status"] == "done"


def test_tombstone_removes_entity() -> None:
    backend = FakeBackend()
    backend.push(
        [
            build_event(
                collection="tickets",
                entity_id="t1",
                actor_id="a",
                actor_seq=1,
                payload={"status": "todo"},
            ),
            build_event(
                collection="tickets", entity_id="t1", actor_id="a", actor_seq=2, op="tombstone"
            ),
        ]
    )
    assert "t1" not in backend.snapshot("tickets")


def test_pull_respects_cursor_and_collection() -> None:
    backend = FakeBackend()
    backend.push(
        [
            build_event(collection="tickets", actor_id="a", actor_seq=1),
            build_event(collection="comments", actor_id="a", actor_seq=2),
        ]
    )
    assert len(backend.pull(since=1)) == 1
    assert len(backend.pull(collection="tickets")) == 1
