"""The dependency graph over ticket_relationships (E15-T2 / MOD-29, D-27).

Folded from the blocks family only (blocking / blocked-by); related/duplicate are
symmetric and excluded. dependency_path_find guards defensively against a legacy
cycle (one the v0.1 create-guard would reject but that pre-guard data may hold):
it returns a structured cycle naming the offending nodes, never a looped or
partial path. Proven on focused graphs AND at the JobWinAI dataset's scale.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.graph import blocks_adjacency, dependency_graph_get, dependency_path_find
from kantaq_core.tracker import TrackerService
from kantaq_db.models import Project, Ticket, TicketRelationship, Workspace
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.linear_fixture import build_linear_export

ACTOR = "mbr_graph00000001"


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


@pytest.fixture
def service(session: Session) -> TrackerService:
    return TrackerService(session, actor_id=ACTOR, source="app", now=FakeClock().now)


@pytest.fixture
def project(service: TrackerService, session: Session) -> Project:
    ws = Workspace(name="Graph Workspace")
    session.add(ws)
    session.commit()
    return service.create_project(workspace_id=ws.id, name="P")


def _ticket(service: TrackerService, project: Project, title: str) -> Ticket:
    return service.create_ticket(project_id=project.id, title=title)


# ----------------------------------------------------------------- correctness


def test_blocks_chain_is_a_path(
    service: TrackerService, project: Project, session: Session
) -> None:
    a = _ticket(service, project, "a")
    b = _ticket(service, project, "b")
    c = _ticket(service, project, "c")
    service.add_relation(a.id, b.id, "blocking")  # a blocks b
    service.add_relation(b.id, c.id, "blocking")  # b blocks c

    result = dependency_path_find(session, a.id, c.id)
    assert result.found
    assert result.path == (a.id, b.id, c.id)
    assert result.cycle is None


def test_blocked_by_contributes_the_reverse_arc(
    service: TrackerService, project: Project, session: Session
) -> None:
    a = _ticket(service, project, "a")
    b = _ticket(service, project, "b")
    # "b blocked-by a" is the same fact as "a blocks b" → an a→b arc.
    service.add_relation(b.id, a.id, "blocked-by")
    result = dependency_path_find(session, a.id, b.id)
    assert result.found and result.path == (a.id, b.id)


def test_related_and_duplicate_are_excluded_from_pathing(
    service: TrackerService, project: Project, session: Session
) -> None:
    a = _ticket(service, project, "a")
    b = _ticket(service, project, "b")
    service.add_relation(a.id, b.id, "related")
    service.add_relation(a.id, b.id, "duplicate")
    # Symmetric relations carry no direction — no blocks edge, no path.
    assert blocks_adjacency(session) == {}
    assert dependency_path_find(session, a.id, b.id).found is False


def test_unreachable_target_is_found_false(
    service: TrackerService, project: Project, session: Session
) -> None:
    a = _ticket(service, project, "a")
    b = _ticket(service, project, "b")
    result = dependency_path_find(session, a.id, b.id)
    assert result.found is False and result.path == () and result.cycle is None


def test_graph_get_whole_rooted_and_depth_bounded(
    service: TrackerService, project: Project, session: Session
) -> None:
    a = _ticket(service, project, "a")
    b = _ticket(service, project, "b")
    c = _ticket(service, project, "c")
    d = _ticket(service, project, "d")
    service.add_relation(a.id, b.id, "blocking")
    service.add_relation(b.id, c.id, "blocking")
    service.add_relation(c.id, d.id, "blocking")

    whole = dependency_graph_get(session)
    assert set(whole.nodes) == {a.id, b.id, c.id, d.id}
    assert len(whole.edges) == 3

    rooted = dependency_graph_get(session, root_ticket_id=a.id)
    assert set(rooted.nodes) == {a.id, b.id, c.id, d.id}

    shallow = dependency_graph_get(session, root_ticket_id=a.id, depth=1)
    assert set(shallow.nodes) == {a.id, b.id}  # only one hop from a
    assert shallow.edges == ((a.id, b.id),)


def test_legacy_cycle_is_detected_and_named(
    service: TrackerService, project: Project, session: Session
) -> None:
    a = _ticket(service, project, "a")
    b = _ticket(service, project, "b")
    c = _ticket(service, project, "c")
    service.add_relation(a.id, b.id, "blocking")
    service.add_relation(b.id, c.id, "blocking")
    # The v0.1 engine would REJECT this (it closes a cycle); insert it directly to
    # simulate legacy pre-guard data.
    with pytest.raises(Exception):  # noqa: B017,PT011 - the guard rejects the cycle
        service.add_relation(c.id, a.id, "blocking")
    session.add(TicketRelationship(from_id=c.id, to_id=a.id, type="blocking"))
    session.commit()

    result = dependency_path_find(session, a.id, c.id)
    assert result.found is False
    assert result.cycle is not None
    assert set(result.cycle) == {a.id, b.id, c.id}  # the offending nodes named


# --------------------------------------------------- the JobWinAI-scale proof


def _seed_jobwin_tickets(session: Session) -> list[str]:
    """Seed the synthetic JobWinAI corpus's 269 tickets (ids only matter here)."""
    ws = Workspace(name="JobWinAI")
    session.add(ws)
    session.commit()
    project = Project(workspace_id=ws.id, name="JobWinAI")
    session.add(project)
    session.commit()
    export = build_linear_export()
    tickets = [
        Ticket(project_id=project.id, title=row.get("title", f"JW-{i}"))
        for i, row in enumerate(export["tickets"])
    ]
    session.add_all(tickets)
    session.commit()
    return [t.id for t in tickets]


def test_path_find_and_graph_on_the_jobwinai_relation_set(session: Session) -> None:
    ids = _seed_jobwin_tickets(session)
    assert len(ids) == 269  # the JobWinAI count

    # A 30-edge critical path across distinct tickets (the longest blocking chain).
    chain = ids[:31]
    session.add_all(
        TicketRelationship(from_id=chain[i], to_id=chain[i + 1], type="blocking")
        for i in range(len(chain) - 1)
    )
    # Some branches off the chain (realistic messiness), none cyclic.
    session.add(TicketRelationship(from_id=chain[5], to_id=ids[200], type="blocking"))
    session.add(TicketRelationship(from_id=chain[10], to_id=ids[201], type="blocking"))
    session.commit()

    result = dependency_path_find(session, chain[0], chain[-1])
    assert result.found
    assert result.path == tuple(chain)  # the full 31-node blocking path
    assert result.cycle is None

    rooted = dependency_graph_get(session, root_ticket_id=chain[0])
    assert chain[-1] in rooted.nodes and ids[200] in rooted.nodes


def test_cycle_detect_holds_on_the_jobwinai_set_without_breaking_clean_paths(
    session: Session,
) -> None:
    ids = _seed_jobwin_tickets(session)
    # A clean chain a→b→c.
    session.add(TicketRelationship(from_id=ids[0], to_id=ids[1], type="blocking"))
    session.add(TicketRelationship(from_id=ids[1], to_id=ids[2], type="blocking"))
    # A SEPARATE legacy cycle x→y→z→x elsewhere in the 269.
    session.add(TicketRelationship(from_id=ids[100], to_id=ids[101], type="blocking"))
    session.add(TicketRelationship(from_id=ids[101], to_id=ids[102], type="blocking"))
    session.add(TicketRelationship(from_id=ids[102], to_id=ids[100], type="blocking"))
    session.commit()

    # The cycle is detected + named when the search starts inside it.
    cyclic = dependency_path_find(session, ids[100], ids[102])
    assert cyclic.cycle is not None
    assert set(cyclic.cycle) == {ids[100], ids[101], ids[102]}

    # A clean path elsewhere is unaffected (the guard is scoped to what the source
    # reaches — a cycle in a disjoint component never poisons a clean query).
    clean = dependency_path_find(session, ids[0], ids[2])
    assert clean.found and clean.path == (ids[0], ids[1], ids[2]) and clean.cycle is None
