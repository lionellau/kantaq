"""Follow-ups: CRUD, validation, ordering, emit, fold, round-trip (E15-T1).

The MOD-29 v0.3 follow-up slice (FR-E15-1): a ``follow_ups`` collection — a
self-scheduled reminder attached to a ticket (title, optional body, status
open/done/dismissed, optional due_at, provenance). Agent-created follow-ups are
propose-first (covered in the MCP tool + proposal-approve tests); this suite
proves the tracker write path itself — create/update/complete/search, the
fail-closed validation, the due-soonest ordering, and that a follow_up emit
folds back to the row like any other lww collection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.tracker import (
    RecordingSink,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
    fold_entity,
)
from kantaq_core.tracker.service import FOLLOW_UP_STATUSES
from kantaq_db.models import AuditEvent, Project, Ticket, Workspace
from kantaq_test_harness.clock import FakeClock

ACTOR = "mbr_followups0001"


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def service(session: Session, sink: RecordingSink) -> TrackerService:
    return TrackerService(session, actor_id=ACTOR, source="app", sink=sink, now=FakeClock().now)


@pytest.fixture
def ticket(service: TrackerService, session: Session) -> Ticket:
    ws = Workspace(name="Follow-up Workspace")
    session.add(ws)
    session.commit()
    project: Project = service.create_project(workspace_id=ws.id, name="Proj")
    return service.create_ticket(project_id=project.id, title="the work")


# ----------------------------------------------------------------- create/read


def test_create_follow_up_sets_fields_audits_and_emits(
    service: TrackerService, ticket: Ticket, sink: RecordingSink, session: Session
) -> None:
    due = datetime(2026, 9, 1, tzinfo=UTC)
    follow_up = service.create_follow_up(
        ticket_id=ticket.id, title="check the deploy", body="after the migration", due_at=due
    )
    assert follow_up.ticket_id == ticket.id
    assert follow_up.title == "check the deploy"
    assert follow_up.body == "after the migration"
    assert follow_up.status == "open"
    assert follow_up.created_by == ACTOR
    # A default manual provenance records who/when when no agent provenance is given.
    assert follow_up.provenance["origin"] == "manual"
    assert follow_up.provenance["actor_id"] == ACTOR
    rows = session.exec(
        select(AuditEvent).where(AuditEvent.object_ref == f"follow_ups/{follow_up.id}")
    ).all()
    assert [r.action for r in rows] == ["follow_up.create"]
    events = [e for e in sink.events if e.collection == "follow_ups"]
    assert len(events) == 1 and events[0].op == "patch"


def test_create_follow_up_keeps_agent_provenance(service: TrackerService, ticket: Ticket) -> None:
    prov = {"origin": "agent", "actor_id": "agt_1", "captured_at": "2026-06-21T00:00:00"}
    follow_up = service.create_follow_up(ticket_id=ticket.id, title="t", provenance=prov)
    assert follow_up.provenance == prov


def test_create_follow_up_rejects_empty_title(service: TrackerService, ticket: Ticket) -> None:
    with pytest.raises(TrackerValidationError, match="non-empty title"):
        service.create_follow_up(ticket_id=ticket.id, title="   ")


def test_create_follow_up_rejects_unknown_ticket(service: TrackerService) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.create_follow_up(ticket_id="tkt_does_not_exist0000000", title="t")


def test_status_vocabulary_is_the_locked_set() -> None:
    assert FOLLOW_UP_STATUSES == ("open", "done", "dismissed")


# ------------------------------------------------------------------- update


def test_update_follow_up_patches_known_fields(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    due = datetime(2026, 10, 1, tzinfo=UTC)
    updated = service.update_follow_up(f.id, {"title": "t1 (revised)", "due_at": due})
    assert updated.title == "t1 (revised)"
    assert updated.due_at == datetime(2026, 10, 1)  # naive-UTC stored


def test_update_follow_up_rejects_unknown_field(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    # status is deliberately NOT patchable — it moves only through complete().
    with pytest.raises(TrackerValidationError, match="unknown follow-up fields"):
        service.update_follow_up(f.id, {"status": "done"})


def test_update_follow_up_rejects_empty_title(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    with pytest.raises(TrackerValidationError, match="non-empty title"):
        service.update_follow_up(f.id, {"title": "  "})


# ------------------------------------------------------------------- complete


def test_complete_follow_up_resolves_done(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    resolved = service.complete_follow_up(f.id, status="done")
    assert resolved.status == "done"


def test_complete_follow_up_resolves_dismissed(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    resolved = service.complete_follow_up(f.id, status="dismissed")
    assert resolved.status == "dismissed"


def test_complete_follow_up_rejects_bad_status(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    with pytest.raises(TrackerValidationError, match="completes to one of"):
        service.complete_follow_up(f.id, status="open")


def test_complete_follow_up_rejects_already_resolved(
    service: TrackerService, ticket: Ticket
) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="t1")
    service.complete_follow_up(f.id, status="done")
    with pytest.raises(TrackerValidationError, match="only an open one"):
        service.complete_follow_up(f.id, status="dismissed")


def test_get_follow_up_missing_raises(service: TrackerService) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.get_follow_up("flw_missing0000000000000")


# -------------------------------------------------------------------- search


def test_search_orders_due_soonest_then_undated_then_id(
    service: TrackerService, ticket: Ticket
) -> None:
    late = service.create_follow_up(
        ticket_id=ticket.id, title="late", due_at=datetime(2026, 12, 1, tzinfo=UTC)
    )
    soon = service.create_follow_up(
        ticket_id=ticket.id, title="soon", due_at=datetime(2026, 7, 1, tzinfo=UTC)
    )
    undated = service.create_follow_up(ticket_id=ticket.id, title="undated")
    ordered = [f.id for f in service.search_follow_ups(ticket_id=ticket.id)]
    assert ordered == [soon.id, late.id, undated.id]


def test_search_filters_by_status_and_due_before(service: TrackerService, ticket: Ticket) -> None:
    open_one = service.create_follow_up(
        ticket_id=ticket.id, title="open", due_at=datetime(2026, 7, 1, tzinfo=UTC)
    )
    far = service.create_follow_up(
        ticket_id=ticket.id, title="far", due_at=datetime(2026, 12, 1, tzinfo=UTC)
    )
    done = service.create_follow_up(ticket_id=ticket.id, title="done-one")
    service.complete_follow_up(done.id, status="done")

    by_status = service.search_follow_ups(status="done")
    assert [f.id for f in by_status] == [done.id]

    due_soon = service.search_follow_ups(due_before=datetime(2026, 8, 1, tzinfo=UTC))
    ids = {f.id for f in due_soon}
    assert open_one.id in ids and far.id not in ids


def test_search_rejects_unknown_status(service: TrackerService) -> None:
    with pytest.raises(TrackerValidationError, match="unknown follow-up status"):
        service.search_follow_ups(status="archived")


# ------------------------------------------------------------- round-trip + fold


def test_create_complete_round_trip(service: TrackerService, ticket: Ticket) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="round-trip")
    assert service.get_follow_up(f.id).status == "open"
    service.complete_follow_up(f.id, status="done")
    assert service.get_follow_up(f.id).status == "done"


def test_follow_up_emits_fold_back_to_the_row(
    service: TrackerService, ticket: Ticket, sink: RecordingSink
) -> None:
    f = service.create_follow_up(ticket_id=ticket.id, title="foldable", body="b")
    service.complete_follow_up(f.id, status="dismissed")
    # Replaying the emitted follow_ups events reproduces the row's folded state.
    follow_up_events = [e for e in sink.events if e.collection == "follow_ups"]
    state = fold_entity(f.id, follow_up_events)
    assert state is not None
    assert state["status"] == "dismissed"
    assert state["title"] == "foldable"
