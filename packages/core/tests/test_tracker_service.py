"""Tracker domain CRUD, validation, activity, and audit attribution (E12-T1)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.tracker import (
    RecordingSink,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
)
from kantaq_db.models import AuditEvent, Member, Workspace
from kantaq_test_harness.clock import FakeClock

ACTOR = "mbr_actor000001"


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def workspace(session: Session) -> Workspace:
    ws = Workspace(name="Test Workspace")
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return ws


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(session: Session, clock: FakeClock) -> TrackerService:
    return TrackerService(session, actor_id=ACTOR, source="app", now=clock.now)


@pytest.fixture
def project_id(service: TrackerService, workspace: Workspace) -> str:
    return service.create_project(workspace_id=workspace.id, name="Proj").id


def _member(session: Session, workspace: Workspace, email: str = "m@example.com") -> Member:
    member = Member(workspace_id=workspace.id, email=email)
    session.add(member)
    session.commit()
    session.refresh(member)
    return member


# ---------------------------------------------------------------- projects


def test_project_crud_round_trip(service: TrackerService, workspace: Workspace) -> None:
    created = service.create_project(workspace_id=workspace.id, name="  Alpha  ", goal="ship")
    assert created.name == "Alpha"  # stripped
    fetched = service.get_project(created.id)
    assert fetched.goal == "ship"

    updated = service.update_project(created.id, {"status": "paused", "scope": "v1 only"})
    assert (updated.status, updated.scope) == ("paused", "v1 only")
    assert [p.id for p in service.list_projects(workspace_id=workspace.id)] == [created.id]


def test_project_requires_existing_workspace(service: TrackerService) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.create_project(workspace_id="ws_nope", name="Orphan")


def test_project_rejects_unknown_status_and_empty_name(
    service: TrackerService, workspace: Workspace
) -> None:
    with pytest.raises(TrackerValidationError):
        service.create_project(workspace_id=workspace.id, name="X", status="cancelled")
    with pytest.raises(TrackerValidationError):
        service.create_project(workspace_id=workspace.id, name="   ")


def test_project_update_rejects_unknown_fields(service: TrackerService, project_id: str) -> None:
    with pytest.raises(TrackerValidationError, match="unknown project fields"):
        service.update_project(project_id, {"workspace_id": "ws_other"})


# ----------------------------------------------------------------- tickets


def test_ticket_crud_round_trip(service: TrackerService, project_id: str) -> None:
    ticket = service.create_ticket(
        project_id=project_id,
        title="Fix login",
        description="md **body**",
        priority="high",
        labels=["bug", "auth", "bug"],  # duplicate collapses
    )
    assert ticket.labels == ["bug", "auth"]

    updated = service.update_ticket(ticket.id, {"status": "doing", "priority": "urgent"})
    assert (updated.status, updated.priority) == ("doing", "urgent")
    assert service.get_ticket(ticket.id).status == "doing"


def test_ticket_validation_fails_closed(service: TrackerService, project_id: str) -> None:
    create = service.create_ticket
    with pytest.raises(TrackerValidationError):
        create(project_id=project_id, title="x", status="blocked")
    with pytest.raises(TrackerValidationError):
        create(project_id=project_id, title="x", priority="asap")
    with pytest.raises(TrackerValidationError):
        create(project_id=project_id, title="   ")
    with pytest.raises(TrackerValidationError):
        create(project_id=project_id, title="x", labels=["ok", "  "])
    with pytest.raises(TrackerValidationError):
        create(project_id=project_id, title="x", lifecycle_stage="Not A Slug!")
    with pytest.raises(TrackerNotFoundError):
        create(project_id="prj_nope", title="x")


def test_ticket_update_rejects_unknown_fields(service: TrackerService, project_id: str) -> None:
    ticket = service.create_ticket(project_id=project_id, title="T")
    with pytest.raises(TrackerValidationError, match="unknown ticket fields"):
        service.update_ticket(ticket.id, {"created_by": "someone-else"})


def test_assignee_must_be_a_member(
    service: TrackerService, session: Session, workspace: Workspace, project_id: str
) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.create_ticket(project_id=project_id, title="T", assignee="mbr_ghost")
    member = _member(session, workspace)
    ticket = service.create_ticket(project_id=project_id, title="T", assignee=member.id)
    assert ticket.assignee == member.id


def test_parent_integrity(service: TrackerService, workspace: Workspace, project_id: str) -> None:
    parent = service.create_ticket(project_id=project_id, title="Parent")
    child = service.create_ticket(project_id=project_id, title="Child", parent_id=parent.id)
    assert child.parent_id == parent.id

    # self-parent
    with pytest.raises(TrackerValidationError, match="own parent"):
        service.update_ticket(parent.id, {"parent_id": parent.id})
    # cycle: parent -> child -> parent
    with pytest.raises(TrackerValidationError, match="cycle"):
        service.update_ticket(parent.id, {"parent_id": child.id})
    # cross-project parent
    other = service.create_project(workspace_id=workspace.id, name="Other")
    stranger = service.create_ticket(project_id=other.id, title="Stranger")
    with pytest.raises(TrackerValidationError, match="same project"):
        service.update_ticket(child.id, {"parent_id": stranger.id})


def test_list_tickets_filters(
    service: TrackerService, session: Session, workspace: Workspace, project_id: str
) -> None:
    member = _member(session, workspace)
    t1 = service.create_ticket(
        project_id=project_id, title="A", status="todo", labels=["bug"], lifecycle_stage="build"
    )
    t2 = service.create_ticket(
        project_id=project_id, title="B", status="doing", assignee=member.id, labels=["ux"]
    )
    other = service.create_project(workspace_id=workspace.id, name="Other")
    service.create_ticket(project_id=other.id, title="C", status="todo")

    assert {t.id for t in service.list_tickets(project_id=project_id)} == {t1.id, t2.id}
    assert [t.id for t in service.list_tickets(status="doing")] == [t2.id]
    assert [t.id for t in service.list_tickets(assignee=member.id)] == [t2.id]
    assert [t.id for t in service.list_tickets(label="bug")] == [t1.id]
    assert [t.id for t in service.list_tickets(stage="build")] == [t1.id]
    assert service.list_tickets(project_id=project_id, status="todo", label="ux") == []


# ---------------------------------------------------------------- comments


def test_comments_append_and_list_in_order(service: TrackerService, project_id: str) -> None:
    ticket = service.create_ticket(project_id=project_id, title="T")
    first = service.add_comment(ticket.id, "first")
    second = service.add_comment(ticket.id, "second")
    assert [c.id for c in service.list_comments(ticket.id)] == [first.id, second.id]
    assert first.author_actor_id == ACTOR


def test_comment_requires_body_and_ticket(service: TrackerService, project_id: str) -> None:
    ticket = service.create_ticket(project_id=project_id, title="T")
    with pytest.raises(TrackerValidationError):
        service.add_comment(ticket.id, "   ")
    with pytest.raises(TrackerNotFoundError):
        service.add_comment("tkt_nope", "hello")


# ---------------------------------------------------------------- activity


def test_status_change_writes_activity(service: TrackerService, project_id: str) -> None:
    ticket = service.create_ticket(project_id=project_id, title="T")
    service.update_ticket(ticket.id, {"status": "doing"})
    service.add_comment(ticket.id, "note")

    feed = service.activity(ticket.id)
    actions = [entry.action for entry in feed]
    assert actions == ["ticket.create", "ticket.update", "comment.create"]
    status_change = feed[1]
    assert status_change.before is not None and status_change.before["status"] == "todo"
    assert status_change.after is not None and status_change.after["status"] == "doing"


def test_every_mutation_is_audited_with_the_actor(
    service: TrackerService, session: Session, workspace: Workspace
) -> None:
    project = service.create_project(workspace_id=workspace.id, name="P")
    ticket = service.create_ticket(project_id=project.id, title="T")
    service.update_ticket(ticket.id, {"priority": "high"})
    service.add_comment(ticket.id, "c")

    rows = session.exec(select(AuditEvent)).all()
    assert {row.action for row in rows} == {
        "project.create",
        "ticket.create",
        "ticket.update",
        "comment.create",
    }
    assert all(row.actor_id == ACTOR and row.source == "app" for row in rows)


def test_timestamps_come_from_the_injected_clock(
    service: TrackerService, clock: FakeClock, project_id: str
) -> None:
    ticket = service.create_ticket(project_id=project_id, title="T")
    created = ticket.updated_at
    clock.advance(3600)
    updated = service.update_ticket(ticket.id, {"title": "T2"})
    assert (updated.updated_at - created).total_seconds() == pytest.approx(3600)


def test_no_sink_means_no_events_but_writes_still_apply(
    session: Session, workspace: Workspace
) -> None:
    service = TrackerService(session, actor_id=ACTOR)  # sink=None: solo mode
    project = service.create_project(workspace_id=workspace.id, name="P")
    assert service.get_project(project.id).name == "P"


def test_sink_receives_create_and_patch_events(
    session: Session, workspace: Workspace, clock: FakeClock
) -> None:
    sink = RecordingSink()
    service = TrackerService(session, actor_id=ACTOR, sink=sink, now=clock.now)
    project = service.create_project(workspace_id=workspace.id, name="P")
    ticket = service.create_ticket(project_id=project.id, title="T")
    service.update_ticket(ticket.id, {"status": "done"})
    service.add_comment(ticket.id, "c")

    kinds = [(e.collection, e.op) for e in sink.events]
    assert kinds == [
        ("projects", "patch"),
        ("tickets", "patch"),
        ("tickets", "patch"),
        ("comments", "append"),
    ]
    patch = sink.events[2]
    assert patch.entity_id == ticket.id
    assert set(patch.payload) == {"status", "updated_at"}
