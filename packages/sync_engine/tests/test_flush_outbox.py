"""E05-T1 durability proofs: the offline-aware outbox flush (MOD-26 §B1).

Each case is deterministic — a single in-memory replica over a FakeBackend whose
``offline`` flag is the partition primitive. Together they cover the four B1
holes the design review flagged: exactly-once on reconnect (NFR-E05-1), a
partition never losing a write, the dropped-ack reconcile, and a never-acceptable
event leaving the outbox instead of being re-pushed forever.
"""

from __future__ import annotations

from sqlmodel import select

from kantaq_db import EventLog, Ticket
from kantaq_sync_engine import (
    SYNC_STATE_REJECTED,
    Backoff,
    SyncEngine,
    VerifyContext,
    VerifyingBackend,
    pending_rows,
    row_to_event,
)
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, memory_replica


def _ticket(replica: Replica) -> str:
    """Create a project + ticket offline (no sync); return the ticket id."""
    with replica.session() as session:
        service = replica.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = service.create_ticket(project_id=project.id, title="T", status="todo")
        session.commit()
        return ticket.id


def test_flush_drains_and_is_exactly_once() -> None:
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    _ticket(alice)  # two pending events: the project create + the ticket create

    result = alice.sync.flush_outbox()

    assert result.committed == 2
    assert result.drained
    assert alice.sync.pending_count() == 0
    assert len(backend) == 2

    # Re-flushing commits nothing new — the reconcile pass sees them already
    # committed and the outbox is empty (exactly-once, NFR-E05-1).
    again = alice.sync.flush_outbox()
    assert again.committed == 0
    assert again.drained
    assert len(backend) == 2


def test_partition_retains_the_outbox_then_drains_on_reconnect() -> None:
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    _ticket(alice)

    backend.offline = True
    slept: list[float] = []
    backoff = Backoff(base_seconds=0.01, factor=2.0, cap_seconds=0.1, max_attempts=4)
    result = alice.sync.flush_outbox(backoff=backoff, sleeper=slept.append)

    # It backed off the bounded number of times and gave up WITHOUT losing data:
    # the durable outbox still holds both writes.
    assert result.attempts == 4
    assert not result.drained
    assert slept == [backoff.delay(1), backoff.delay(2), backoff.delay(3)]
    assert alice.sync.pending_count() == 2
    assert len(backend) == 0

    # Reconnect: the same writes flush, each committed exactly once.
    backend.offline = False
    reconnected = alice.sync.flush_outbox(backoff=backoff, sleeper=slept.append)
    assert reconnected.drained
    assert alice.sync.pending_count() == 0
    assert len(backend) == 2


def test_dropped_ack_reconcile_backfills_without_re_pushing() -> None:
    """The server commits, the connection drops before the ack: the local rows
    stay pending though they are committed server-side. flush_outbox must
    backfill from the backend's own log BEFORE pushing, so it neither
    double-commits nor mis-orders the pending tail (B1)."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    _ticket(alice)

    # Simulate the dropped ack: push the pending events straight to the backend
    # (committed server-side) while the local rows stay committed_rev = NULL.
    with alice.session() as session:
        events = [row_to_event(row) for row in pending_rows(session)]
    committed = backend.push(events)
    assert len(committed) == 2
    assert alice.sync.pending_count() == 2  # local still thinks they are pending

    result = alice.sync.flush_outbox()

    assert result.reconciled == 2  # backfilled from the backend's own log
    assert result.submitted == 0  # nothing re-pushed
    assert len(backend) == 2  # exactly-once: not double-committed
    assert alice.sync.pending_count() == 0


def test_unverifiable_event_leaves_the_outbox_and_reverts() -> None:
    """A never-acceptable event (here: unsigned under a require-signature peer)
    is moved to a terminal state and its optimistic effect reverted, so it
    leaves the outbox instead of wedging pending_count forever (B1)."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    ticket_id = _ticket(alice)
    alice.sync.flush_outbox()  # the creates commit cleanly

    # An offline edit, then a peer that requires signatures (our writes are
    # unsigned in this harness) — the edit can never be accepted.
    with alice.session() as session:
        alice.service(session).update_ticket(ticket_id, {"status": "done"})
        session.commit()
    assert alice.sync.pending_count() == 1

    verifying = VerifyingBackend(
        inner=backend,
        context=lambda: VerifyContext(roots={}, grants={}, now=0, require_signature=True),
    )
    engine = SyncEngine(alice.db, verifying, actor_id=alice.actor_id)
    result = engine.flush_outbox()

    assert result.rejected == 1
    assert result.drained
    assert engine.pending_count() == 0  # no zombie retry
    with alice.session() as session:
        # The optimistic "done" was reverted to the committed value.
        assert session.get(Ticket, ticket_id).status == "todo"
        terminal = session.exec(
            select(EventLog).where(EventLog.sync_state == SYNC_STATE_REJECTED)
        ).all()
        assert len(terminal) == 1


def _agent_writes_a_ticket(alice: Replica, agent_id: str) -> str:
    """An Agent member on Alice's machine authors through the same local log —
    what the MCP gateway does when it stores a propose-first write."""
    from kantaq_core.tracker import TrackerService
    from kantaq_sync_engine import EventLogSink

    with alice.session() as session:
        service = TrackerService(
            session,
            actor_id=agent_id,
            source="mcp",
            sink=EventLogSink(session, agent_id),
            now=alice.clock.now,
        )
        project = service.create_project(workspace_id=WORKSPACE_ID, name="Agent's")
        session.commit()
        return project.id


def test_a_caller_bound_backend_withholds_another_actors_events() -> None:
    """DEBT-49: the self-hosted server binds actor == the token's member, so an
    Agent member's local write can never be accepted. It must not be submitted —
    one refusal used to take the whole batch down and wedge the human's own
    writes out of the team forever."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    _ticket(alice)  # two events authored by alice
    agent_id = "mbr_agent00000000000000000"[:26]
    _agent_writes_a_ticket(alice, agent_id)  # one event authored by the agent

    engine = SyncEngine(
        alice.db,
        backend,
        actor_id=alice.actor_id,
        workspace_id=WORKSPACE_ID,
        caller_bound_actor=True,
    )
    result = engine.flush_outbox()

    assert result.committed == 2  # alice's own writes reached the team
    assert result.withheld == 1
    assert result.drained  # nothing pushable is left behind
    assert {entry.event.actor_id for entry in backend.pull()} == {alice.actor_id}
    with alice.session() as session:
        # Withheld, NOT rejected: the local write stands and stays pending, so
        # the agent's work is still there for the human to approve.
        held = [r for r in pending_rows(session) if r.actor_id == agent_id]
        assert len(held) == 1
        assert held[0].sync_state == "pending"


def test_without_caller_binding_every_actor_still_pushes() -> None:
    """The Supabase backend resolves the acting member by verified email, so it
    accepts an Agent member's events — the withholding is opt-in, not the rule."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    _ticket(alice)
    _agent_writes_a_ticket(alice, "mbr_agent00000000000000000"[:26])

    result = alice.sync.flush_outbox()

    assert result.committed == 3
    assert result.withheld == 0
