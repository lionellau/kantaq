"""Milestones: CRUD, junction integrity, activity, emit, fold (E14-T2/T3).

The MOD-20 v0.3 milestone slice (FR-E14-3): a flat ``milestones`` entity scoped
to a project (name, description, optional target_date, status active/complete/
archived) and the ``ticket_milestones`` junction grouping a project's tickets.
Integrity is *valid status, existing project, same-project membership, no
duplicate membership*. The fold property proves both collections sync like any
other lww collection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.tracker import (
    MILESTONE_STATUSES,
    RecordingSink,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
    fold_entity,
)
from kantaq_db.models import AuditEvent, Project, TicketMilestone, Workspace
from kantaq_test_harness.clock import FakeClock

ACTOR = "mbr_milestones001"


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
def project(service: TrackerService, session: Session) -> Project:
    ws = Workspace(name="Milestone Workspace")
    session.add(ws)
    session.commit()
    return service.create_project(workspace_id=ws.id, name="Proj")


# ----------------------------------------------------------------- create/read


def test_create_milestone_sets_fields_audits_and_emits(
    service: TrackerService, project: Project, sink: RecordingSink, session: Session
) -> None:
    target = datetime(2026, 9, 1, tzinfo=UTC)
    milestone = service.create_milestone(
        project_id=project.id, name="v1.0 launch", description="ship it", target_date=target
    )
    assert milestone.name == "v1.0 launch"
    assert milestone.description == "ship it"
    assert milestone.status == "active"
    assert milestone.created_by == ACTOR
    # The audit row landed on the milestone (its own activity object_ref).
    rows = session.exec(
        select(AuditEvent).where(AuditEvent.object_ref == f"milestones/{milestone.id}")
    ).all()
    assert [r.action for r in rows] == ["milestone.create"]
    # A lww patch event was emitted on the milestones collection.
    events = [e for e in sink.events if e.collection == "milestones"]
    assert len(events) == 1 and events[0].op == "patch"


def test_create_milestone_rejects_empty_name(service: TrackerService, project: Project) -> None:
    with pytest.raises(TrackerValidationError, match="non-empty name"):
        service.create_milestone(project_id=project.id, name="   ")


def test_create_milestone_rejects_unknown_status(service: TrackerService, project: Project) -> None:
    with pytest.raises(TrackerValidationError, match="unknown milestone status"):
        service.create_milestone(project_id=project.id, name="m", status="shipped")


def test_create_milestone_rejects_unknown_project(service: TrackerService) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.create_milestone(project_id="prj_does_not_exist000000000", name="m")


def test_status_vocabulary_is_the_locked_set() -> None:
    assert MILESTONE_STATUSES == ("active", "complete", "archived")


# ------------------------------------------------------------------- update


def test_update_milestone_patches_known_fields(service: TrackerService, project: Project) -> None:
    m = service.create_milestone(project_id=project.id, name="m1")
    updated = service.update_milestone(m.id, {"status": "complete", "name": "m1 (done)"})
    assert updated.status == "complete"
    assert updated.name == "m1 (done)"


def test_update_milestone_rejects_unknown_field(service: TrackerService, project: Project) -> None:
    m = service.create_milestone(project_id=project.id, name="m1")
    with pytest.raises(TrackerValidationError, match="unknown milestone fields"):
        service.update_milestone(m.id, {"project_id": "moved"})


def test_update_milestone_rejects_bad_status(service: TrackerService, project: Project) -> None:
    m = service.create_milestone(project_id=project.id, name="m1")
    with pytest.raises(TrackerValidationError, match="unknown milestone status"):
        service.update_milestone(m.id, {"status": "nope"})


def test_get_milestone_missing_raises(service: TrackerService) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.get_milestone("mst_missing00000000000000")


def test_list_milestones_orders_dated_first_then_id_stable(
    service: TrackerService, project: Project
) -> None:
    late = service.create_milestone(
        project_id=project.id, name="late", target_date=datetime(2026, 12, 1, tzinfo=UTC)
    )
    early = service.create_milestone(
        project_id=project.id, name="early", target_date=datetime(2026, 6, 1, tzinfo=UTC)
    )
    undated = service.create_milestone(project_id=project.id, name="undated")
    ordered = [m.id for m in service.list_milestones(project_id=project.id)]
    assert ordered == [early.id, late.id, undated.id]


def test_list_milestones_can_exclude_archived(service: TrackerService, project: Project) -> None:
    service.create_milestone(project_id=project.id, name="active one")
    service.create_milestone(project_id=project.id, name="old", status="archived")
    names = {m.name for m in service.list_milestones(project_id=project.id, include_archived=False)}
    assert names == {"active one"}


# ------------------------------------------------------------------- delete


def test_delete_milestone_tombstones_memberships_first(
    service: TrackerService, project: Project, sink: RecordingSink, session: Session
) -> None:
    m = service.create_milestone(project_id=project.id, name="m")
    t = service.create_ticket(project_id=project.id, title="T")
    membership = service.add_ticket_to_milestone(t.id, m.id)

    service.delete_milestone(m.id)

    # The membership row is gone (no dangling FK) and the milestone is gone.
    assert session.get(TicketMilestone, membership.id) is None
    with pytest.raises(TrackerNotFoundError):
        service.get_milestone(m.id)
    # Both a junction tombstone and a milestone tombstone were emitted.
    milestone_events = [e for e in sink.events if e.collection == "milestones"]
    junction_events = [e for e in sink.events if e.collection == "ticket_milestones"]
    assert milestone_events[-1].op == "tombstone"
    assert junction_events[-1].op == "tombstone"


# ------------------------------------------------- membership (junction) rules


def test_add_ticket_to_milestone_emits_and_audits(
    service: TrackerService, project: Project, sink: RecordingSink, session: Session
) -> None:
    m = service.create_milestone(project_id=project.id, name="m")
    t = service.create_ticket(project_id=project.id, title="T")
    membership = service.add_ticket_to_milestone(t.id, m.id)
    assert membership.created_by == ACTOR
    # The activity lands on the ticket so it shows in the ticket feed.
    rows = session.exec(select(AuditEvent).where(AuditEvent.object_ref == f"tickets/{t.id}")).all()
    assert "milestone.add_ticket" in {r.action for r in rows}
    events = [e for e in sink.events if e.collection == "ticket_milestones"]
    assert len(events) == 1 and events[0].op == "patch"


def test_add_ticket_rejects_duplicate_membership(service: TrackerService, project: Project) -> None:
    m = service.create_milestone(project_id=project.id, name="m")
    t = service.create_ticket(project_id=project.id, title="T")
    service.add_ticket_to_milestone(t.id, m.id)
    with pytest.raises(TrackerValidationError, match="already in that milestone"):
        service.add_ticket_to_milestone(t.id, m.id)


def test_add_ticket_rejects_cross_project_membership(
    service: TrackerService, project: Project, session: Session
) -> None:
    other = service.create_project(workspace_id=project.workspace_id, name="Other")
    m = service.create_milestone(project_id=project.id, name="m")
    foreign = service.create_ticket(project_id=other.id, title="elsewhere")
    with pytest.raises(TrackerValidationError, match="own project"):
        service.add_ticket_to_milestone(foreign.id, m.id)


def test_add_ticket_to_missing_milestone_raises(service: TrackerService, project: Project) -> None:
    t = service.create_ticket(project_id=project.id, title="T")
    with pytest.raises(TrackerNotFoundError):
        service.add_ticket_to_milestone(t.id, "mst_missing00000000000000")


def test_remove_membership_tombstones(
    service: TrackerService, project: Project, sink: RecordingSink, session: Session
) -> None:
    m = service.create_milestone(project_id=project.id, name="m")
    t = service.create_ticket(project_id=project.id, title="T")
    membership = service.add_ticket_to_milestone(t.id, m.id)
    service.remove_ticket_from_milestone(membership.id)
    assert session.get(TicketMilestone, membership.id) is None
    junction = [e for e in sink.events if e.collection == "ticket_milestones"]
    assert junction[-1].op == "tombstone"


def test_milestones_for_ticket_and_tickets_for_milestone(
    service: TrackerService, project: Project
) -> None:
    m1 = service.create_milestone(project_id=project.id, name="m1")
    m2 = service.create_milestone(project_id=project.id, name="m2")
    t = service.create_ticket(project_id=project.id, title="T")
    service.add_ticket_to_milestone(t.id, m1.id)
    service.add_ticket_to_milestone(t.id, m2.id)
    assert {m.id for m in service.milestones_for_ticket(t.id)} == {m1.id, m2.id}
    assert [tk.id for tk in service.tickets_for_milestone(m1.id)] == [t.id]


# ------------------------------------------------------------------- fold


def test_milestone_events_fold_back_to_the_row(
    service: TrackerService, project: Project, sink: RecordingSink
) -> None:
    m = service.create_milestone(project_id=project.id, name="m")
    service.update_milestone(m.id, {"status": "complete"})
    events = [e for e in sink.events if e.collection == "milestones" and e.entity_id == m.id]
    folded = fold_entity(m.id, events)
    assert folded is not None
    assert folded["status"] == "complete"
    # After delete the fold collapses to a tombstone (no row).
    service.delete_milestone(m.id)
    events = [e for e in sink.events if e.collection == "milestones" and e.entity_id == m.id]
    assert fold_entity(m.id, events) is None
