"""Demo seed (FR-E02-5)."""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_db.models import Comment, Member, Project, Ticket, Workspace
from kantaq_db.seed import DEMO_WORKSPACE_NAME, seed_demo


def test_seed_creates_a_usable_demo(temp_sqlite: Engine) -> None:
    SQLModel.metadata.create_all(temp_sqlite)
    summary = seed_demo(temp_sqlite)

    assert summary.created
    assert summary.members == 1
    assert summary.projects == 1
    assert summary.tickets == 5
    assert summary.comments == 1

    with Session(temp_sqlite) as session:
        ws = session.exec(select(Workspace).where(Workspace.name == DEMO_WORKSPACE_NAME)).one()
        project = session.exec(select(Project)).one()
        assert project.workspace_id == ws.id
        # every ticket hangs off the demo project (FK integrity)
        tickets = list(session.exec(select(Ticket)))
        assert {t.project_id for t in tickets} == {project.id}
        # statuses span the board, not toy data
        assert {t.status for t in tickets} >= {"todo", "doing", "done"}
        owner = session.exec(select(Member).where(Member.role == "Owner")).one()
        comment = session.exec(select(Comment)).one()
        assert comment.author_actor_id == owner.id


def test_seed_is_idempotent(temp_sqlite: Engine) -> None:
    SQLModel.metadata.create_all(temp_sqlite)
    first = seed_demo(temp_sqlite)
    second = seed_demo(temp_sqlite)

    assert first.created
    assert not second.created
    assert second.tickets == 5

    with Session(temp_sqlite) as session:
        assert len(list(session.exec(select(Workspace)))) == 1
        assert len(list(session.exec(select(Ticket)))) == 5
