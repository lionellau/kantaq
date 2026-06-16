"""E05-T2.5: resolving a conflict_record supersedes the field + flips to resolved.

resolve_conflict emits a superseding field write (base_rev = the record's
head_rev) AND the status=resolved flip, committed together through the atomic
RPC; sticky-resolved in the fold. (The atomic rebase_required reject when the
field head moved is enforced by the events.sql RPC — T2.6 — and cross-checked on
EphemeralPostgres.)
"""

from __future__ import annotations

from kantaq_db import ConflictRecord, Ticket, new_ulid
from kantaq_sync_engine import (
    Event,
    conflict_record_id,
    entity_base_rev,
    insert_event,
    next_actor_seq,
    refold_entity,
)
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, memory_replica


def _seed_open_conflict(alice: Replica) -> tuple[str, str]:
    """Create a ticket (status=todo) and an event-sourced open conflict_record on
    its status field (keep_a=doing, keep_b=todo). Returns (ticket_id, conflict_id)."""
    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = alice.service(session).create_ticket(
            project_id=project.id, title="T", status="todo"
        )
        tid = ticket.id
        session.commit()
    alice.sync.flush_outbox()  # the create commits

    with alice.session() as session:
        head = entity_base_rev(session, "tickets", tid)
        assert head is not None
        cr_id = conflict_record_id(tid, "status", [head])
        cr_event = Event(
            event_id=new_ulid(),
            collection="conflict_records",
            entity_id=cr_id,
            actor_id=alice.actor_id,
            actor_seq=next_actor_seq(session, alice.actor_id),
            op="patch",
            payload={
                "workspace_id": WORKSPACE_ID,
                "collection": "tickets",
                "entity_id": tid,
                "field": "status",
                "contending_revisions": [head],
                "candidate_values": {"keep_a": "doing", "keep_b": "todo"},
                "base_rev": 0,
                "head_rev": head,
                "actor": alice.actor_id,
                "status": "open",
            },
        )
        committed = alice.sync._backend.commit_events([cr_event])
        insert_event(session, cr_event, committed_rev=committed[0].revision)
        refold_entity(session, "conflict_records", cr_id)
        session.commit()
        assert session.get(ConflictRecord, cr_id).status == "open"
    return tid, cr_id


def test_resolve_supersedes_the_field_and_marks_the_record_resolved() -> None:
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    tid, cr_id = _seed_open_conflict(alice)

    result = alice.sync.resolve_conflict(cr_id, "keep-A")

    assert result.resolved and not result.rebase_required
    with alice.session() as session:
        assert session.get(Ticket, tid).status == "doing"  # superseded to keep-A
        rec = session.get(ConflictRecord, cr_id)
        assert rec.status == "resolved"
        assert rec.resolved_choice == "keep-A"
        assert rec.resolved_by == alice.actor_id

    # The resolution is audited as a distinct actor (DoD; arch fact).
    from sqlmodel import select

    from kantaq_db import AuditEvent

    with alice.session() as session:
        rows = [
            a for a in session.exec(select(AuditEvent)).all() if a.action == "conflict.resolved"
        ]
        assert len(rows) == 1
        assert rows[0].actor_id == alice.actor_id
        assert rows[0].object_ref == f"conflict_records/{cr_id}"

    # Idempotent + sticky: resolving again is a no-op (never reopens).
    again = alice.sync.resolve_conflict(cr_id, "keep-B")
    assert again.resolved
    with alice.session() as session:
        assert session.get(Ticket, tid).status == "doing"  # unchanged — sticky resolved
