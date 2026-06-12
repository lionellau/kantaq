"""E04-T2 — push/pull: idempotent re-push, LWW by commit order, cursor resume."""

from __future__ import annotations

import pytest
from sqlmodel import select

from kantaq_db import AuditEvent, Comment, Ticket
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, Replica


def _create_project_and_ticket(replica: Replica) -> tuple[str, str]:
    with replica.session() as session:
        service = replica.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = service.create_ticket(project_id=project.id, title="Shared ticket")
        return project.id, ticket.id


def _sync_all(*replicas: Replica) -> None:
    for replica in replicas:
        replica.sync.push()
    for replica in replicas:
        replica.sync.pull()


def test_push_marks_local_events_committed(alice: Replica, backend: FakeBackend) -> None:
    _create_project_and_ticket(alice)
    assert alice.sync.pending_count() == 2

    result = alice.sync.push()

    assert (result.submitted, result.committed) == (2, 2)
    assert alice.sync.pending_count() == 0
    assert len(backend) == 2


def test_repush_never_duplicates(alice: Replica, backend: FakeBackend) -> None:
    """NFR-E04-2: the same events submitted again commit nothing new."""
    _create_project_and_ticket(alice)
    alice.sync.push()
    before = len(backend)

    again = alice.sync.push()

    assert again.committed == 0
    assert len(backend) == before


def test_pull_applies_remote_events_to_the_replica(alice: Replica, bob: Replica) -> None:
    project_id, ticket_id = _create_project_and_ticket(alice)
    alice.sync.push()

    result = bob.sync.pull()

    assert result.applied == 2
    with bob.session() as session:
        ticket = session.get(Ticket, ticket_id)
        assert ticket is not None and ticket.title == "Shared ticket"
        assert ticket.project_id == project_id


def test_round_trip_reflects_within_one_push_pull(alice: Replica, bob: Replica) -> None:
    """NFR-E04-1's semantic half: one push+pull cycle and the change is there
    (the ≤2 s wall-clock half is the UI's 2 s polling interval, MOD-14)."""
    _, ticket_id = _create_project_and_ticket(alice)
    _sync_all(alice, bob)

    with bob.session() as session:
        bob.service(session).update_ticket(ticket_id, {"status": "doing"})
    bob.sync.push()
    alice.sync.pull()

    with alice.session() as session:
        ticket = session.get(Ticket, ticket_id)
        assert ticket is not None and ticket.status == "doing"


def test_own_events_pulled_back_reconcile_without_reapplying(
    alice: Replica, backend: FakeBackend
) -> None:
    _create_project_and_ticket(alice)
    alice.sync.push()

    result = alice.sync.pull()

    assert result.applied == 0
    assert result.own_reconciled == 2
    with alice.session() as session:
        # No duplicate rows, no extra audit: still exactly one ticket.create.
        actions = [a.action for a in session.exec(select(AuditEvent)).all()]
        assert actions.count("ticket.create") == 1
        assert "ticket.sync" not in actions


def test_lww_by_commit_order_even_when_the_later_writer_pulls_late(
    alice: Replica, bob: Replica
) -> None:
    """The D-05 hard case: B's *later-committed* write must survive B pulling
    A's *earlier-committed* write afterwards. Naive apply would regress B to
    A's value; the re-fold keeps commit order."""
    _, ticket_id = _create_project_and_ticket(alice)
    _sync_all(alice, bob)

    with alice.session() as session:
        alice.service(session).update_ticket(ticket_id, {"status": "doing"})
    with bob.session() as session:
        bob.service(session).update_ticket(ticket_id, {"status": "done"})

    alice.sync.push()  # commits first → earlier revision
    bob.sync.push()  # commits second → later revision wins (D-05)

    bob.sync.pull()  # receives Alice's earlier event *after* writing its own
    alice.sync.pull()

    for replica in (alice, bob):
        with replica.session() as session:
            ticket = session.get(Ticket, ticket_id)
            assert ticket is not None and ticket.status == "done", replica.name


def test_comments_replicate_append_only(alice: Replica, bob: Replica) -> None:
    _, ticket_id = _create_project_and_ticket(alice)
    _sync_all(alice, bob)

    with bob.session() as session:
        comment = bob.service(session).add_comment(ticket_id, "from bob")
    _sync_all(bob, alice)

    with alice.session() as session:
        replicated = session.get(Comment, comment.id)
        assert replicated is not None and replicated.body == "from bob"
        assert replicated.author_actor_id == bob.actor_id


def test_ingested_remote_events_are_audited_as_sync(alice: Replica, bob: Replica) -> None:
    _create_project_and_ticket(alice)
    alice.sync.push()
    bob.sync.pull()

    with bob.session() as session:
        rows = session.exec(select(AuditEvent)).all()
        sync_rows = [row for row in rows if row.source == "sync"]
        assert {row.action for row in sync_rows} == {"project.sync", "ticket.sync"}
        # Attribution stays with the original actor; the source says it synced.
        assert {row.actor_id for row in sync_rows} == {alice.actor_id}


class FlakyBackend:
    """Delegates to a FakeBackend but drops the first pull mid-flight."""

    def __init__(self, inner: FakeBackend) -> None:
        self._inner = inner
        self.failures_left = 1

    def push(self, events: object) -> list[object]:  # pragma: no cover - passthrough
        return self._inner.push(events)  # type: ignore[arg-type]

    def pull(self, collection: str | None = None, since: int = 0) -> list[object]:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise ConnectionError("connection dropped mid-pull")
        return self._inner.pull(collection, since)  # type: ignore[return-value]

    def snapshot(self, collection: str) -> dict[str, dict[str, object]]:
        return self._inner.snapshot(collection)  # type: ignore[return-value]


def test_cursor_resumes_after_a_dropped_connection(
    tmp_path: object, alice: Replica, backend: FakeBackend
) -> None:
    from kantaq_sync_engine import SyncEngine

    _create_project_and_ticket(alice)
    alice.sync.push()

    # Bob's engine rides a flaky connection this time.
    from pathlib import Path

    from kantaq_test_harness.replica import make_replica

    bob = make_replica(Path(str(tmp_path)), "bobflaky", backend)
    flaky = FlakyBackend(backend)
    bob_sync = SyncEngine(bob.db, flaky, actor_id=bob.actor_id)

    with pytest.raises(ConnectionError):
        bob_sync.pull()
    assert bob_sync.cursor() == 0  # nothing acked: the transaction rolled back

    result = bob_sync.pull()  # resume from the same cursor

    assert result.applied == 2
    assert bob_sync.cursor() == 2
    repeat = bob_sync.pull()  # and the overlap window is dedup-safe
    assert (repeat.received, repeat.applied) == (0, 0)
