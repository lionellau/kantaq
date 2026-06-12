"""E04-T1 — the append-only event log: actor_seq, dedup floor, ordering."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from kantaq_db import EventLog
from kantaq_sync_engine import (
    Event,
    entity_rows,
    insert_event,
    next_actor_seq,
    pending_rows,
)
from kantaq_test_harness.replica import WORKSPACE_ID, Replica


def _event(actor_id: str, seq: int, **overrides: object) -> Event:
    base: dict[str, object] = {
        "event_id": f"evt{actor_id[4:14]}{seq:04d}".ljust(26, "0"),
        "collection": "tickets",
        "entity_id": "tkt_x".ljust(26, "0"),
        "actor_id": actor_id,
        "actor_seq": seq,
        "op": "patch",
        "payload": {"title": f"v{seq}"},
    }
    base.update(overrides)
    return Event(**base)  # type: ignore[arg-type]


def test_actor_seq_is_monotonic_per_actor(alice: Replica) -> None:
    with alice.session() as session:
        service = alice.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        service.create_ticket(project_id=project.id, title="T1")
        service.create_ticket(project_id=project.id, title="T2")

        rows = session.exec(select(EventLog)).all()
        seqs = sorted(row.actor_seq for row in rows)
        assert seqs == [1, 2, 3]  # project + 2 tickets, no gaps, one actor
        assert {row.actor_id for row in rows} == {alice.actor_id}
        assert next_actor_seq(session, alice.actor_id) == 4


def test_tracker_write_and_its_event_share_one_transaction(alice: Replica) -> None:
    with alice.session() as session:
        service = alice.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = service.create_ticket(project_id=project.id, title="T")
    with alice.session() as session:
        events = entity_rows(session, "tickets", ticket.id)
        assert [e.op for e in events] == ["patch"]
        assert events[0].payload["title"] == "T"
        assert events[0].committed_rev is None  # local, not yet pushed


def test_duplicate_actor_seq_is_impossible(alice: Replica) -> None:
    """The UNIQUE constraint is the hard floor under NFR-E04-2."""
    with alice.session() as session:
        insert_event(session, _event(alice.actor_id, 1))
        session.commit()
    with alice.session() as session, pytest.raises(IntegrityError):
        insert_event(session, _event(alice.actor_id, 1, event_id="evt_other".ljust(26, "0")))
        session.commit()


def test_resolution_order_is_commit_order_then_pending(alice: Replica) -> None:
    with alice.session() as session:
        # Inserted out of order on purpose: committed 7 and 3, plus one pending.
        insert_event(session, _event("mbr_remote".ljust(26, "0"), 1), committed_rev=7)
        insert_event(session, _event("mbr_remote2".ljust(26, "0"), 1), committed_rev=3)
        insert_event(session, _event(alice.actor_id, 1))
        session.commit()

        ordered = entity_rows(session, "tickets", "tkt_x".ljust(26, "0"))
        assert [row.committed_rev for row in ordered] == [3, 7, None]
        assert pending_rows(session)[0].actor_id == alice.actor_id
