"""E05-T1 integration proof (RISK-04): partition heal converges on the offline path.

Two replicas share one backend, each through its own PartitionLink so one can be
partitioned alone. While A is partitioned it edits offline (the durable outbox
retains the write); B edits a different field and commits. On heal both
flush_outbox + apply_inbox and converge to identical state with the different
fields auto-merged (FR-E05-3) — exactly-once, nothing lost, no conflict_record
needed because the edits touch different scalars.
"""

from __future__ import annotations

from kantaq_db import Ticket
from kantaq_sync_engine import Backoff
from kantaq_test_harness.backend import FakeBackend, PartitionLink
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, memory_replica

_INSTANT = Backoff(max_attempts=2, base_seconds=0.0)


def _converged(replica: Replica, ticket_id: str) -> tuple[str, str]:
    with replica.session() as session:
        row = session.get(Ticket, ticket_id)
        assert row is not None
        return row.status, row.priority


def test_partition_heal_auto_merges_different_fields() -> None:
    backend = FakeBackend()
    link_a = PartitionLink(backend)
    link_b = PartitionLink(backend)
    alice = memory_replica("alice", link_a)
    bob = memory_replica("bob", link_b)

    # Alice creates a project + ticket and syncs; Bob picks it up.
    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = alice.service(session).create_ticket(
            project_id=project.id, title="T", status="todo", priority="low"
        )
        ticket_id = ticket.id
        session.commit()
    alice.sync.flush_outbox()
    bob.sync.apply_inbox()

    # Partition Alice. She edits status offline: the flush backs off and the
    # write stays in the durable outbox (no loss).
    link_a.online = False
    with alice.session() as session:
        alice.service(session).update_ticket(ticket_id, {"status": "doing"})
        session.commit()
    offline = alice.sync.flush_outbox(backoff=_INSTANT, sleeper=lambda _s: None)
    assert not offline.drained
    assert alice.sync.pending_count() == 1

    # Bob (still connected) edits a DIFFERENT field and commits.
    with bob.session() as session:
        bob.service(session).update_ticket(ticket_id, {"priority": "high"})
        session.commit()
    bob.sync.flush_outbox()

    # Heal Alice: her edit flushes, both ingest, and they converge.
    link_a.online = True
    alice.sync.flush_outbox()
    alice.sync.apply_inbox()
    bob.sync.apply_inbox()

    backend_state = backend.snapshot("tickets")[ticket_id]
    assert (backend_state["status"], backend_state["priority"]) == ("doing", "high")
    assert _converged(alice, ticket_id) == ("doing", "high")  # offline edit survived
    assert _converged(bob, ticket_id) == ("doing", "high")  # different field merged
    assert alice.sync.pending_count() == 0
