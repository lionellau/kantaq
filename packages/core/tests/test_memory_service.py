"""Memory domain CRUD, links, vocabularies, expiry, and audit (E13-T1/T2)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.memory import (
    DOMAIN_VISIBILITIES,
    MEMORY_SPACES,
    MEMORY_VISIBILITIES,
    REVIEW_STATUSES,
    MemoryNotFoundError,
    MemoryService,
    MemoryValidationError,
    domain_visibility,
)
from kantaq_core.tracker import RecordingSink
from kantaq_db.models import AuditEvent, MemoryLink, Project, Ticket, Workspace
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
def ticket_id(session: Session) -> str:
    ws = Workspace(name="W")
    session.add(ws)
    session.commit()
    project = Project(workspace_id=ws.id, name="P")
    session.add(project)
    session.commit()
    ticket = Ticket(project_id=project.id, title="T")
    session.add(ticket)
    session.commit()
    return ticket.id


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def service(session: Session, clock: FakeClock, sink: RecordingSink) -> MemoryService:
    return MemoryService(session, actor_id=ACTOR, source="app", sink=sink, now=clock.now)


def _audit_rows(session: Session, action: str) -> list[AuditEvent]:
    rows = session.exec(select(AuditEvent).where(AuditEvent.action == action)).all()
    return sorted(rows, key=lambda r: r.id)


# ------------------------------------------------------------------- entries


def test_create_round_trip_with_defaults(service: MemoryService) -> None:
    entry = service.create_entry(title="  Auth decision  ")
    assert entry.title == "Auth decision"
    assert entry.type == "note"
    assert entry.source == "manual"
    assert entry.space == "workspace"
    assert entry.visibility == "team"
    assert entry.confidence == "medium"
    assert entry.review_status == "draft"
    assert entry.created_by == ACTOR


def test_create_fills_provenance_who_when_how(service: MemoryService, clock: FakeClock) -> None:
    entry = service.create_entry(title="T", source="import")
    assert entry.provenance["origin"] == "import"
    assert entry.provenance["actor_id"] == ACTOR
    # captured_at is the injected clock, naive-UTC encoded like the store.
    assert entry.provenance["captured_at"] == (
        clock.now().astimezone(UTC).replace(tzinfo=None).isoformat()
    )


def test_create_keeps_caller_provenance(service: MemoryService) -> None:
    entry = service.create_entry(
        title="T", provenance={"origin": "manual", "detail": "from standup"}
    )
    assert entry.provenance["detail"] == "from standup"
    assert entry.provenance["origin"] == "manual"
    assert "actor_id" in entry.provenance  # defaults still complete the rest


@pytest.mark.parametrize(
    "field",
    ["type", "source", "space", "confidence"],
)
def test_create_rejects_unknown_vocabulary(service: MemoryService, field: str) -> None:
    with pytest.raises(MemoryValidationError, match=f"unknown {field}"):
        service.create_entry(title="T", **{field: "bogus"})


def test_create_rejects_unknown_visibility(service: MemoryService) -> None:
    with pytest.raises(MemoryValidationError, match="unknown visibility"):
        service.create_entry(title="T", visibility="public")


def test_create_rejects_empty_title(service: MemoryService) -> None:
    with pytest.raises(MemoryValidationError, match="non-empty title"):
        service.create_entry(title="   ")


def test_create_rejects_oversize_title_and_body(service: MemoryService) -> None:
    with pytest.raises(MemoryValidationError, match="title exceeds"):
        service.create_entry(title="x" * 501)
    with pytest.raises(MemoryValidationError, match="body exceeds"):
        service.create_entry(title="T", body="x" * 100_001)


def test_linked_entities_validated_and_deduped(service: MemoryService) -> None:
    entry = service.create_entry(
        title="T", linked_entities=["projects/abc", " projects/abc ", "releases/v1"]
    )
    assert entry.linked_entities == ["projects/abc", "releases/v1"]
    with pytest.raises(MemoryValidationError, match="not a collection/id"):
        service.create_entry(title="T", linked_entities=["not a ref"])
    with pytest.raises(MemoryValidationError, match="list of strings"):
        service.update_entry(entry.id, {"linked_entities": [1]})


def test_provenance_validation_fails_closed(service: MemoryService) -> None:
    with pytest.raises(MemoryValidationError, match="unknown provenance keys"):
        service.create_entry(title="T", provenance={"password": "hunter2"})
    with pytest.raises(MemoryValidationError, match="values must be strings"):
        service.create_entry(title="T", provenance={"detail": 42})
    entry = service.create_entry(title="T")
    with pytest.raises(MemoryValidationError, match="must be an object"):
        service.update_entry(entry.id, {"provenance": "nope"})


def test_expires_at_normalized_to_naive_utc(service: MemoryService) -> None:
    aware = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
    entry = service.create_entry(title="T", expires_at=aware)
    assert entry.expires_at == datetime(2026, 6, 12, 10, 0)


def test_update_round_trip(service: MemoryService) -> None:
    entry = service.create_entry(title="T")
    updated = service.update_entry(
        entry.id, {"body": "new body", "confidence": "high", "review_status": "stale"}
    )
    assert updated.body == "new body"
    assert updated.confidence == "high"
    assert updated.review_status == "stale"


def test_update_rejects_unknown_fields(service: MemoryService) -> None:
    entry = service.create_entry(title="T")
    with pytest.raises(MemoryValidationError, match="unknown memory fields"):
        service.update_entry(entry.id, {"created_by": "someone-else"})


def test_visibility_is_immutable(service: MemoryService) -> None:
    """Loosening local→team is the v0.2 promotion; tightening strands copies."""
    local = service.create_entry(title="private", visibility="local")
    team = service.create_entry(title="shared", visibility="team")
    for entry, target in ((local, "team"), (team, "local")):
        with pytest.raises(MemoryValidationError, match="visibility is immutable"):
            service.update_entry(entry.id, {"visibility": target})


def test_review_status_writes_limited_in_v01(service: MemoryService) -> None:
    entry = service.create_entry(title="T")
    for blocked in ("proposed", "approved", "rejected"):
        with pytest.raises(MemoryValidationError, match="promotion workflow"):
            service.update_entry(entry.id, {"review_status": blocked})
    with pytest.raises(MemoryValidationError, match="unknown review_status"):
        service.update_entry(entry.id, {"review_status": "bogus"})


def test_get_and_list_not_found(service: MemoryService) -> None:
    with pytest.raises(MemoryNotFoundError, match="no such memory entr"):
        service.get_entry("mem_missing")
    with pytest.raises(MemoryNotFoundError):
        service.update_entry("mem_missing", {"body": "x"})
    with pytest.raises(MemoryNotFoundError):
        service.delete_entry("mem_missing")


def test_list_filters_and_keyword_search(service: MemoryService) -> None:
    service.create_entry(title="Sync design", type="decision", space="codebase")
    service.create_entry(title="Release ritual", type="note", space="release")
    service.create_entry(title="Auth constraint", body="JWT only", type="constraint")

    assert [e.title for e in service.list_entries(space="codebase")] == ["Sync design"]
    assert [e.title for e in service.list_entries(type="note")] == ["Release ritual"]
    assert [e.title for e in service.list_entries(q="jwt")] == ["Auth constraint"]
    assert [e.title for e in service.list_entries(q="SYNC")] == ["Sync design"]
    assert service.list_entries(q="nothing-matches") == []
    # Newest first (ULID order).
    titles = [e.title for e in service.list_entries()]
    assert titles == ["Auth constraint", "Release ritual", "Sync design"]


def test_expired_entries_filtered_by_the_clock(service: MemoryService, clock: FakeClock) -> None:
    keeper = service.create_entry(title="keeper")
    service.create_entry(title="fades", expires_at=clock.now())  # expires "now"
    clock.advance(60)

    assert [e.title for e in service.list_entries()] == [keeper.title]
    both = {e.title for e in service.list_entries(include_expired=True)}
    assert both == {"keeper", "fades"}


def test_list_review_status_filter(service: MemoryService) -> None:
    entry = service.create_entry(title="aging")
    service.create_entry(title="fresh")
    service.update_entry(entry.id, {"review_status": "stale"})
    assert [e.title for e in service.list_entries(review_status="stale")] == ["aging"]


# --------------------------------------------------------------------- links


def test_create_and_link_round_trip(service: MemoryService, ticket_id: str) -> None:
    entry = service.create_entry(title="Context")
    link = service.link(entry.id, ticket_id, "explains the design")
    assert (link.ticket_id, link.memory_id) == (ticket_id, entry.id)
    assert link.reason == "explains the design"
    assert link.created_by == ACTOR
    assert link.visibility == "team"  # inherited from the entry

    assert [row.id for row in service.links_for_entry(entry.id)] == [link.id]
    linked = service.linked_memory(ticket_id)
    assert [(lk.reason, en.title) for lk, en in linked] == [("explains the design", "Context")]


def test_link_inherits_local_visibility(service: MemoryService, ticket_id: str) -> None:
    entry = service.create_entry(title="private", visibility="local")
    link = service.link(entry.id, ticket_id, "private context")
    assert link.visibility == "local"


def test_link_integrity_fails_closed(service: MemoryService, ticket_id: str) -> None:
    entry = service.create_entry(title="T")
    with pytest.raises(MemoryNotFoundError, match="no such ticket"):
        service.link(entry.id, "tkt_missing", "r")
    with pytest.raises(MemoryNotFoundError, match="no such memory entr"):
        service.link("mem_missing", ticket_id, "r")
    with pytest.raises(MemoryValidationError, match="non-empty reason"):
        service.link(entry.id, ticket_id, "   ")
    with pytest.raises(MemoryValidationError, match="reason exceeds"):
        service.link(entry.id, ticket_id, "x" * 501)
    service.link(entry.id, ticket_id, "first")
    with pytest.raises(MemoryValidationError, match="already linked"):
        service.link(entry.id, ticket_id, "second")


def test_linked_memory_requires_the_ticket(service: MemoryService) -> None:
    with pytest.raises(MemoryNotFoundError, match="no such ticket"):
        service.linked_memory("tkt_missing")


def test_linked_memory_filters_expired(
    service: MemoryService, ticket_id: str, clock: FakeClock
) -> None:
    entry = service.create_entry(title="fades", expires_at=clock.now())
    service.link(entry.id, ticket_id, "soon gone")
    clock.advance(60)
    assert service.linked_memory(ticket_id) == []
    assert len(service.linked_memory(ticket_id, include_expired=True)) == 1


def test_delete_removes_entry_and_links(
    service: MemoryService, session: Session, ticket_id: str
) -> None:
    entry = service.create_entry(title="T")
    service.link(entry.id, ticket_id, "r")
    service.delete_entry(entry.id)
    with pytest.raises(MemoryNotFoundError):
        service.get_entry(entry.id)
    assert session.exec(select(MemoryLink)).all() == []


# --------------------------------------------------------------------- audit


def test_every_write_is_audited(service: MemoryService, session: Session, ticket_id: str) -> None:
    entry = service.create_entry(title="T")
    service.update_entry(entry.id, {"body": "b"})
    service.link(entry.id, ticket_id, "r")
    service.delete_entry(entry.id)

    for action in ("memory.create", "memory.update", "memory.link", "memory.delete"):
        assert len(_audit_rows(session, action)) == 1, action


def test_team_audit_carries_snapshots(service: MemoryService, session: Session) -> None:
    entry = service.create_entry(title="T")
    service.update_entry(entry.id, {"body": "new"})
    create_row = _audit_rows(session, "memory.create")[0]
    update_row = _audit_rows(session, "memory.update")[0]
    assert create_row.object_ref == f"memory_entries/{entry.id}"
    assert create_row.after is not None and create_row.after["title"] == "T"
    assert update_row.before is not None and update_row.before["body"] == ""
    assert update_row.after is not None and update_row.after["body"] == "new"


def test_local_audit_is_content_free(
    service: MemoryService, session: Session, ticket_id: str
) -> None:
    """Existence is auditable (§6.13); private content never is — audit syncs."""
    entry = service.create_entry(title="secret plan", visibility="local")
    service.update_entry(entry.id, {"body": "the details"})
    service.link(entry.id, ticket_id, "private reason")
    service.delete_entry(entry.id)

    for action in ("memory.create", "memory.update", "memory.delete"):
        row = _audit_rows(session, action)[0]
        assert row.before is None and row.after is None, action
        assert row.object_ref == f"memory_entries/{entry.id}"
    link_row = _audit_rows(session, "memory.link")[0]
    # Audited on the entry, not the ticket: the private association must not
    # enter the ticket's activity feed.
    assert link_row.object_ref == f"memory_entries/{entry.id}"
    assert link_row.before is None and link_row.after is None


def test_team_link_audits_on_the_ticket(
    service: MemoryService, session: Session, ticket_id: str
) -> None:
    entry = service.create_entry(title="T")
    service.link(entry.id, ticket_id, "r")
    link_row = _audit_rows(session, "memory.link")[0]
    assert link_row.object_ref == f"tickets/{ticket_id}"
    assert link_row.after is not None and link_row.after["reason"] == "r"


# ------------------------------------------------------------------ mapping


@pytest.mark.parametrize(
    ("visibility", "review_status", "space", "expected"),
    [
        ("local", "draft", "workspace", "private_local"),
        ("local", "stale", "agent_run", "private_local"),
        ("local", "draft", "agent_run", "agent_run_private"),
        ("team", "draft", "workspace", "personal_synced"),
        ("team", "stale", "project", "personal_synced"),
        ("team", "proposed", "workspace", "proposal_context"),
        # approved splits by share scope (the sixth state, E13-T5): a workspace
        # note shares workspace-wide; every other space is project-scoped.
        ("team", "approved", "workspace", "shared_workspace"),
        ("team", "approved", "project", "shared_project"),
        ("team", "approved", "ticket", "shared_project"),
        ("team", "approved", "codebase", "shared_project"),
        ("team", "approved", "release", "shared_project"),
        # agent_run can't carry a team-approved row in practice (agent runs are
        # local), but the mapping is total — it reads as project scope.
        ("team", "approved", "agent_run", "shared_project"),
        # A human-declined proposal (proposed→rejected) is a reachable team state;
        # it reads as personal_synced (the MOD-19 catch-all: everything team that
        # is not proposed/approved), NOT a sixth label.
        ("team", "rejected", "workspace", "personal_synced"),
        ("team", "rejected", "project", "personal_synced"),
    ],
)
def test_domain_visibility_single_source_of_truth(
    visibility: str, review_status: str, space: str, expected: str
) -> None:
    assert domain_visibility(visibility, review_status, space) == expected


def test_domain_visibility_range_is_the_closed_vocabulary() -> None:
    """Every (visibility, review_status, space) triple folds to exactly one of
    the six DOMAIN_VISIBILITIES — the range is the closed vocabulary, nothing
    outside it, and every label is reachable (the single-source-of-truth set)."""
    seen = {
        domain_visibility(visibility, review_status, space)
        for visibility in MEMORY_VISIBILITIES
        for review_status in REVIEW_STATUSES
        for space in MEMORY_SPACES
    }
    assert seen == set(DOMAIN_VISIBILITIES)
    assert len(DOMAIN_VISIBILITIES) == 6
