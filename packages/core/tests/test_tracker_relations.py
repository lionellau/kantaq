"""Typed ticket relationships: integrity, activity, emit, fold, sync (E12-T3).

The MOD-03 v0.1 relation rules (FR-E12-3): five types, two symmetric
(``related``/``duplicate``), an inverse pair (``blocking``⇔``blocked-by``), and
``caused-by``; integrity is *no self-link, no duplicate (any equivalent
spelling), no cycle* in the dependency families (``blocks``/``causes``). The
fold property and a two-replica sync round-trip prove the edge syncs like any
collection.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.tracker import (
    RELATIONSHIP_TYPES,
    RecordingSink,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
    fold_entity,
)
from kantaq_db.models import AuditEvent, Workspace
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, make_replica

ACTOR = "mbr_relations0001"


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
def tickets(service: TrackerService, session: Session) -> dict[str, str]:
    """A workspace + project + three tickets a/b/c, returned by their ids."""
    ws = Workspace(name="Rel Workspace")
    session.add(ws)
    session.commit()
    project = service.create_project(workspace_id=ws.id, name="Proj")
    return {
        name: service.create_ticket(project_id=project.id, title=name.upper()).id
        for name in ("a", "b", "c")
    }


# --------------------------------------------------------------- happy path


def test_create_and_list_from_both_ends(service: TrackerService, tickets: dict[str, str]) -> None:
    rel = service.add_relation(tickets["a"], tickets["b"], "blocking")
    assert rel.type == "blocking"
    # The edge is visible from either endpoint's relations query.
    assert [r.id for r in service.relations_for(tickets["a"])] == [rel.id]
    assert [r.id for r in service.relations_for(tickets["b"])] == [rel.id]
    assert service.relations_for(tickets["c"]) == []


def test_every_type_can_be_created(service: TrackerService, tickets: dict[str, str]) -> None:
    # Distinct endpoint pairs so none collide on the dedup key.
    a, b, c = tickets["a"], tickets["b"], tickets["c"]
    service.add_relation(a, b, "related")
    service.add_relation(a, c, "duplicate")
    service.add_relation(b, c, "blocked-by")
    extra = service.create_ticket(project_id=service.get_ticket(a).project_id, title="D")
    service.add_relation(b, extra.id, "caused-by")
    # blocking with a fresh pair so it doesn't invert an existing blocked-by.
    service.add_relation(c, extra.id, "blocking")
    assert {r.type for r in service.relations_for(b)} == {"related", "blocked-by", "caused-by"}


# ------------------------------------------------------------- integrity


def test_unknown_type_is_rejected(service: TrackerService, tickets: dict[str, str]) -> None:
    with pytest.raises(TrackerValidationError, match="unknown relationship type"):
        service.add_relation(tickets["a"], tickets["b"], "supersedes")


def test_self_link_is_rejected(service: TrackerService, tickets: dict[str, str]) -> None:
    with pytest.raises(TrackerValidationError, match="cannot relate to itself"):
        service.add_relation(tickets["a"], tickets["a"], "related")


def test_missing_endpoint_is_not_found(service: TrackerService, tickets: dict[str, str]) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.add_relation(tickets["a"], "tkt_ghost00000000000000000", "related")


def test_exact_duplicate_is_rejected(service: TrackerService, tickets: dict[str, str]) -> None:
    service.add_relation(tickets["a"], tickets["b"], "related")
    with pytest.raises(TrackerValidationError, match="already have"):
        service.add_relation(tickets["a"], tickets["b"], "related")


def test_symmetric_duplicate_in_either_order_is_rejected(
    service: TrackerService, tickets: dict[str, str]
) -> None:
    """``related``/``duplicate`` are symmetric: (A,B) == (B,A)."""
    for rel_type in ("related", "duplicate"):
        a, b = tickets["a"], tickets["b"]
        service.add_relation(a, b, rel_type)
        with pytest.raises(TrackerValidationError, match="already have"):
            service.add_relation(b, a, rel_type)
        # reset for the next type
        service.remove_relation(service.relations_for(a)[0].id)


def test_inverse_blocking_pair_is_one_fact(
    service: TrackerService, tickets: dict[str, str]
) -> None:
    """``A blocking B`` and ``B blocked-by A`` are the same dependency."""
    service.add_relation(tickets["a"], tickets["b"], "blocking")
    with pytest.raises(TrackerValidationError, match="already have"):
        service.add_relation(tickets["b"], tickets["a"], "blocked-by")


def test_blocks_cycle_is_rejected(service: TrackerService, tickets: dict[str, str]) -> None:
    a, b, c = tickets["a"], tickets["b"], tickets["c"]
    service.add_relation(a, b, "blocking")  # a -> b
    service.add_relation(b, c, "blocking")  # b -> c
    with pytest.raises(TrackerValidationError, match="blocks cycle"):
        service.add_relation(c, a, "blocking")  # c -> a closes the loop


def test_blocked_by_can_close_a_blocking_cycle(
    service: TrackerService, tickets: dict[str, str]
) -> None:
    """The cycle check is over the family, not the spelling: a ``blocked-by``
    arc that completes a ``blocking`` loop is caught too."""
    a, b = tickets["a"], tickets["b"]
    service.add_relation(a, b, "blocking")  # a blocks b
    with pytest.raises(TrackerValidationError, match="blocks cycle"):
        service.add_relation(a, b, "blocked-by")  # b blocks a → 2-cycle


def test_reciprocal_causation_is_a_cycle(service: TrackerService, tickets: dict[str, str]) -> None:
    a, b = tickets["a"], tickets["b"]
    service.add_relation(a, b, "caused-by")  # b causes a
    with pytest.raises(TrackerValidationError, match="causes cycle"):
        service.add_relation(b, a, "caused-by")  # a causes b → mutual causation


def test_symmetric_types_never_cycle(service: TrackerService, tickets: dict[str, str]) -> None:
    """related/duplicate form no order, so a 'reverse' of a different pair is fine."""
    a, b, c = tickets["a"], tickets["b"], tickets["c"]
    service.add_relation(a, b, "related")
    service.add_relation(b, c, "related")
    service.add_relation(c, a, "related")  # would be a cycle for a directed type
    assert len(service.relations_for(a)) == 2


def test_cross_workspace_relation_is_rejected(
    service: TrackerService, session: Session, tickets: dict[str, str]
) -> None:
    other_ws = Workspace(name="Other")
    session.add(other_ws)
    session.commit()
    other_project = service.create_project(workspace_id=other_ws.id, name="Other Proj")
    foreign = service.create_ticket(project_id=other_project.id, title="Foreign")
    with pytest.raises(TrackerValidationError, match="same workspace"):
        service.add_relation(tickets["a"], foreign.id, "related")


# ----------------------------------------------------- activity, emit, fold


def test_relation_writes_activity_on_the_from_ticket(
    service: TrackerService, session: Session, tickets: dict[str, str]
) -> None:
    rel = service.add_relation(tickets["a"], tickets["b"], "blocking")
    service.remove_relation(rel.id)
    actions_on_a = [
        row.action
        for row in session.exec(
            select(AuditEvent).where(AuditEvent.object_ref == f"tickets/{tickets['a']}")
        ).all()
    ]
    assert "relation.create" in actions_on_a
    assert "relation.delete" in actions_on_a
    # The from-ticket carries the activity; the to-ticket's feed stays clean.
    actions_on_b = [
        row.action
        for row in session.exec(
            select(AuditEvent).where(AuditEvent.object_ref == f"tickets/{tickets['b']}")
        ).all()
    ]
    assert "relation.create" not in actions_on_b


def test_emit_stream_folds_to_the_row_then_to_none(
    service: TrackerService, sink: RecordingSink, tickets: dict[str, str]
) -> None:
    """Create emits a ``patch`` that folds to the row snapshot; delete emits a
    ``tombstone`` that folds the entity away (MOD-04 source-of-truth rule)."""
    from kantaq_core import audit

    rel = service.add_relation(tickets["a"], tickets["b"], "blocking")
    after_create = [e for e in sink.events if e.collection == "ticket_relationships"]
    assert [e.op for e in after_create] == ["patch"]
    assert fold_entity(rel.id, after_create) == audit.snapshot(rel)

    service.remove_relation(rel.id)
    all_events = [e for e in sink.events if e.collection == "ticket_relationships"]
    assert [e.op for e in all_events] == ["patch", "tombstone"]
    assert fold_entity(rel.id, all_events) is None


def test_remove_relation_missing_id_is_not_found(
    service: TrackerService, tickets: dict[str, str]
) -> None:
    with pytest.raises(TrackerNotFoundError):
        service.remove_relation("rel_ghost0000000000000000")


def test_relation_type_set_is_locked() -> None:
    assert RELATIONSHIP_TYPES == ("related", "blocked-by", "blocking", "duplicate", "caused-by")


# --------------------------------------------------------- sync round-trip


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def alice(tmp_path: Path, backend: FakeBackend) -> Replica:
    return make_replica(tmp_path, "alice", backend)


@pytest.fixture
def bob(tmp_path: Path, backend: FakeBackend) -> Replica:
    return make_replica(tmp_path, "bob", backend)


def test_relation_syncs_to_another_replica(
    alice: Replica, bob: Replica, backend: FakeBackend
) -> None:
    """A relation created on one replica reaches another through the event log
    (the collection is on the full sync surface, E12-T3)."""
    with alice.session() as session:
        service = alice.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        a_id = service.create_ticket(project_id=project.id, title="A").id
        b_id = service.create_ticket(project_id=project.id, title="B").id
        rel_id = service.add_relation(a_id, b_id, "blocking").id

    alice.sync.push()
    bob.sync.pull()

    with bob.session() as session:
        replicated = bob.service(session).relations_for(a_id)
        assert [r.id for r in replicated] == [rel_id]
        assert replicated[0].type == "blocking"
