"""NFR-E12-1: list queries stay under 100 ms on a realistic backlog.

The reference backlog size is 269 tickets (the shape of a real small-team
export — see MOD-03's reference-fixture note), generated synthetically with
SeededRandom so the test is hermetic and the public repo carries no real data.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.tracker import TrackerService
from kantaq_db.models import Project, Ticket, Workspace
from kantaq_test_harness.random import SeededRandom

BACKLOG_SIZE = 269
STATUSES = ("todo", "doing", "done")
PRIORITIES = ("low", "medium", "high", "urgent")
STAGES = ("intake", "design", "build", "review", "done")
LABEL_POOL = ("bug", "ux", "infra", "docs", "agent", "sync")


@pytest.fixture
def seeded_backlog(temp_sqlite: Engine) -> tuple[Engine, str]:
    SQLModel.metadata.create_all(temp_sqlite)
    rng = SeededRandom(42)
    with Session(temp_sqlite) as session:
        workspace = Workspace(name="Perf Workspace")
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, name="Perf Project")
        session.add(project)
        session.flush()
        ticket_ids: list[str] = []
        for i in range(BACKLOG_SIZE):
            labels = sorted({rng.choice(LABEL_POOL) for _ in range(rng.integer(0, 3))})
            parent_id = rng.choice(ticket_ids) if ticket_ids and rng.integer(0, 4) == 0 else None
            ticket = Ticket(
                project_id=project.id,
                title=f"Ticket {i}: {rng.token(16)}",
                description=rng.token(200),
                status=rng.choice(STATUSES),
                priority=rng.choice(PRIORITIES),
                lifecycle_stage=rng.choice(STAGES),
                labels=labels,
                parent_id=parent_id,
            )
            session.add(ticket)
            session.flush()
            ticket_ids.append(ticket.id)
        session.commit()
        return temp_sqlite, project.id


def test_269_ticket_list_under_100ms(seeded_backlog: tuple[Engine, str]) -> None:
    engine, project_id = seeded_backlog
    with Session(engine) as session:
        service = TrackerService(session, actor_id="mbr_perf")
        service.list_tickets(project_id=project_id)  # warm the connection

        started = time.perf_counter()
        rows = service.list_tickets(project_id=project_id)
        elapsed_ms = (time.perf_counter() - started) * 1000

    assert len(rows) == BACKLOG_SIZE
    assert elapsed_ms < 100, f"list took {elapsed_ms:.1f} ms (NFR-E12-1 budget: 100 ms)"


def test_filtered_list_also_under_100ms(seeded_backlog: tuple[Engine, str]) -> None:
    engine, project_id = seeded_backlog
    with Session(engine) as session:
        service = TrackerService(session, actor_id="mbr_perf")
        service.list_tickets(project_id=project_id)

        started = time.perf_counter()
        rows = service.list_tickets(project_id=project_id, status="todo", label="bug")
        elapsed_ms = (time.perf_counter() - started) * 1000

    assert all(t.status == "todo" and "bug" in t.labels for t in rows)
    assert elapsed_ms < 100, f"filtered list took {elapsed_ms:.1f} ms (NFR-E12-1)"
