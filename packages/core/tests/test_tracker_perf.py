"""NFR-E12-1: list queries stay under 100 ms on a realistic backlog.

The reference backlog size is 269 tickets (the shape of a real small-team
export — see MOD-03's reference-fixture note), generated synthetically with
SeededRandom so the test is hermetic and the public repo carries no real data.

We assert the **floor** of several timed samples (``min``), not a single shot.
NFR-E12-1 is a statement about the *operation's* cost, and on a shared CI
runner under coverage tracing a single sample measures whichever scheduling
hiccup happened to land on it (locally the op is ~3 ms; one contended sample
can read 150 ms+). The minimum across a handful of back-to-back runs is the
sample with the least interference — the closest read of what the code
actually costs — while the 100 ms budget itself is unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core import lifecycle
from kantaq_core.tracker import TrackerService
from kantaq_db.models import Project, Ticket, Workspace
from kantaq_test_harness.random import SeededRandom

BACKLOG_SIZE = 269
STATUSES = ("todo", "doing", "done")
PRIORITIES = ("low", "medium", "high", "urgent")
STAGES = lifecycle.STAGE_SLUGS  # the locked MOD-20 taxonomy
LABEL_POOL = ("bug", "ux", "infra", "docs", "agent", "sync")
# Back-to-back timed runs; the floor (min) is the least-interfered read.
_PERF_SAMPLES = 5


def _fastest_ms(op: Callable[[], object]) -> float:
    """The floor wall-clock of ``op`` across ``_PERF_SAMPLES`` runs, in ms."""
    best = float("inf")
    for _ in range(_PERF_SAMPLES):
        started = time.perf_counter()
        op()
        best = min(best, (time.perf_counter() - started) * 1000)
    return best


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

        elapsed_ms = _fastest_ms(lambda: service.list_tickets(project_id=project_id))
        rows = service.list_tickets(project_id=project_id)

    assert len(rows) == BACKLOG_SIZE
    assert elapsed_ms < 100, f"list took {elapsed_ms:.1f} ms (NFR-E12-1 budget: 100 ms)"


def test_filtered_list_also_under_100ms(seeded_backlog: tuple[Engine, str]) -> None:
    engine, project_id = seeded_backlog
    with Session(engine) as session:
        service = TrackerService(session, actor_id="mbr_perf")
        service.list_tickets(project_id=project_id)

        elapsed_ms = _fastest_ms(
            lambda: service.list_tickets(project_id=project_id, status="todo", label="bug")
        )
        rows = service.list_tickets(project_id=project_id, status="todo", label="bug")

    assert all(t.status == "todo" and "bug" in t.labels for t in rows)
    assert elapsed_ms < 100, f"filtered list took {elapsed_ms:.1f} ms (NFR-E12-1)"
