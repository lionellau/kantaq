"""Seed a small, believable demo workspace (FR-E02-5).

``kantaq db seed`` calls ``seed_demo`` to populate a fresh database with a demo
workspace, an owner member, a project, and a handful of tickets across statuses
(plus a comment), so a first run shows real-looking work instead of an empty
backlog. It is idempotent: if the demo workspace already exists it returns the
existing counts without inserting duplicates.

This is a hand-curated minimal set. A richer seed from the sanitized JobWinAI
Linear export (docs/reference) can land with the tracker domain (MOD-03), which
owns realistic backlog fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq_db.models import Comment, Member, Project, Ticket, Workspace

DEMO_WORKSPACE_NAME = "Demo Workspace"

_DEMO_TICKETS: tuple[tuple[str, str, str, str], ...] = (
    # (title, status, priority, lifecycle_stage)
    ("Set up the team workspace", "done", "high", "done"),
    ("Draft the product brief", "doing", "high", "build"),
    ("Wire up loopback MCP gateway", "doing", "medium", "build"),
    ("Design the backlog list view", "todo", "medium", "design"),
    ("Write the quickstart guide", "todo", "low", "intake"),
)


@dataclass(frozen=True)
class SeedSummary:
    workspace_id: str
    members: int
    projects: int
    tickets: int
    comments: int
    created: bool


def seed_demo(engine: Engine) -> SeedSummary:
    """Insert the demo workspace if absent; return counts either way."""
    with Session(engine) as session:
        existing = session.exec(
            select(Workspace).where(Workspace.name == DEMO_WORKSPACE_NAME)
        ).first()
        if existing is not None:
            return _summarize(session, existing.id, created=False)

        workspace = Workspace(name=DEMO_WORKSPACE_NAME)
        session.add(workspace)
        session.flush()  # assign workspace.id for the FKs below

        owner = Member(
            workspace_id=workspace.id,
            email="owner@example.com",
            role="Owner",
        )
        session.add(owner)
        session.flush()

        project = Project(
            workspace_id=workspace.id,
            name="kantaq dogfood",
            goal="Run our own backlog on kantaq.",
            owner=owner.id,
            status="active",
        )
        session.add(project)
        session.flush()

        first_ticket_id: str | None = None
        for title, status, priority, stage in _DEMO_TICKETS:
            ticket = Ticket(
                project_id=project.id,
                title=title,
                status=status,
                priority=priority,
                lifecycle_stage=stage,
                created_by=owner.id,
            )
            session.add(ticket)
            session.flush()
            if first_ticket_id is None:
                first_ticket_id = ticket.id

        assert first_ticket_id is not None
        session.add(
            Comment(
                ticket_id=first_ticket_id,
                author_actor_id=owner.id,
                body="Workspace bootstrapped — let's go.",
            )
        )

        session.commit()
        return _summarize(session, workspace.id, created=True)


def _summarize(session: Session, workspace_id: str, *, created: bool) -> SeedSummary:
    project_ids = [
        p.id for p in session.exec(select(Project).where(Project.workspace_id == workspace_id))
    ]
    members = len(list(session.exec(select(Member).where(Member.workspace_id == workspace_id))))
    tickets = [t for t in session.exec(select(Ticket)) if t.project_id in set(project_ids)]
    ticket_ids = {t.id for t in tickets}
    comments = len([c for c in session.exec(select(Comment)) if c.ticket_id in ticket_ids])
    return SeedSummary(
        workspace_id=workspace_id,
        members=members,
        projects=len(project_ids),
        tickets=len(tickets),
        comments=comments,
        created=created,
    )
