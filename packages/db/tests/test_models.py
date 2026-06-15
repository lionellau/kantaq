"""Model CRUD + SQLite WAL/FK behavior (MOD-02 Domain profile)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from kantaq_db.ids import is_ulid
from kantaq_db.models import (
    Comment,
    Project,
    SkillContainerRow,
    SkillMappingRow,
    Ticket,
    Workspace,
)
from kantaq_db.session import get_engine, sqlite_url


def test_crud_round_trip(temp_sqlite: Engine) -> None:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        ws = Workspace(name="Acme")
        session.add(ws)
        session.flush()
        project = Project(workspace_id=ws.id, name="AcmeApp", goal="ship it")
        session.add(project)
        session.flush()
        ticket = Ticket(project_id=project.id, title="Fix login", labels=["bug", "auth"])
        session.add(ticket)
        session.flush()
        session.add(Comment(ticket_id=ticket.id, author_actor_id="mbr_1", body="on it"))
        session.commit()
        ticket_id = ticket.id

    with Session(temp_sqlite) as session:
        loaded = session.get(Ticket, ticket_id)
        assert loaded is not None
        assert loaded.title == "Fix login"
        assert loaded.labels == ["bug", "auth"]  # JSON round-trips
        assert is_ulid(loaded.id)
        # privacy envelope defaults (D-14)
        assert (loaded.visibility, loaded.hosting_mode, loaded.retention_policy) == (
            "team",
            "plain",
            "standard",
        )
        assert loaded.actor_seq == 0
        comment = session.exec(select(Comment)).one()
        assert comment.ticket_id == ticket_id


def test_sqlite_runs_in_wal_mode(tmp_path: Path) -> None:
    engine = get_engine(sqlite_url(tmp_path / "wal.sqlite"))
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert str(mode).lower() == "wal"


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    engine = get_engine(sqlite_url(tmp_path / "fk.sqlite"))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session, pytest.raises(IntegrityError):
        # Comment references a ticket that does not exist.
        session.add(Comment(ticket_id="tkt_missing", author_actor_id="m", body="x"))
        session.commit()


def test_skill_registry_crud_round_trip(temp_sqlite: Engine) -> None:
    """Container + mapping persist with envelope defaults and JSON round-trip (E17-T4)."""
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        container = SkillContainerRow(
            slug="repo-investigation",
            name="Repo investigation",
            recommended_roles=["code_agent"],
            supported_stages=["implementation"],
            allowed_tools=["role_context_get", "ticket_get"],
            default_write_mode="read",
            risk_level="low",
        )
        session.add(container)
        session.flush()
        mapping = SkillMappingRow(
            container_id=container.id,
            scope="workspace",
            provider="anthropic",
            connection="an MCP-connected coding agent",
            created_by="mbr_1",
        )
        session.add(mapping)
        session.commit()
        container_id, mapping_id = container.id, mapping.id

    with Session(temp_sqlite) as session:
        loaded = session.get(SkillContainerRow, container_id)
        assert loaded is not None
        assert loaded.slug == "repo-investigation"
        # JSON list columns round-trip.
        assert loaded.recommended_roles == ["code_agent"]
        assert loaded.supported_stages == ["implementation"]
        assert loaded.allowed_tools == ["role_context_get", "ticket_get"]
        assert is_ulid(loaded.id)
        # privacy envelope defaults (D-14).
        assert (loaded.visibility, loaded.hosting_mode, loaded.retention_policy) == (
            "team",
            "plain",
            "standard",
        )
        assert loaded.actor_seq == 0
        mapped = session.get(SkillMappingRow, mapping_id)
        assert mapped is not None
        assert mapped.container_id == container_id
        assert mapped.scope == "workspace"
        assert mapped.connection == "an MCP-connected coding agent"


def test_skill_mapping_foreign_key_enforced(tmp_path: Path) -> None:
    engine = get_engine(sqlite_url(tmp_path / "skfk.sqlite"))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session, pytest.raises(IntegrityError):
        # Mapping references a skill container that does not exist.
        session.add(SkillMappingRow(container_id="skc_missing", provider="x"))
        session.commit()
