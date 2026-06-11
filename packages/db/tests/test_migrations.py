"""Migration up/down, model parity, and the schema-version guard (E02-T2)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

import kantaq_db.models  # noqa: F401  (register tables on the metadata)
from kantaq_db import migrations, schema_version
from kantaq_db.parity import reflect_structure
from kantaq_db.session import get_engine

_ALL_TABLES = {
    "workspaces",
    "projects",
    "tickets",
    "comments",
    "members",
    "tokens",
    "audit_events",
    "agent_proposals",
    "schema_version",
}


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrate.sqlite'}"


def _compare_metadata(engine: Engine) -> list[object]:
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "target_metadata": SQLModel.metadata}
        )
        return list(compare_metadata(ctx, SQLModel.metadata))


def test_migrate_from_clean(tmp_path: Path) -> None:
    url = _url(tmp_path)
    migrations.upgrade(url)
    tables = set(inspect(get_engine(url)).get_table_names())
    assert tables >= _ALL_TABLES


def test_migration_matches_models(tmp_path: Path) -> None:
    url = _url(tmp_path)
    migrations.upgrade(url)
    # Autogenerate detects no difference: the migration is the models.
    assert _compare_metadata(get_engine(url)) == []


def test_up_down_up_leaves_zero_drift(tmp_path: Path) -> None:
    url = _url(tmp_path)
    engine = get_engine(url)

    migrations.upgrade(url)
    before = reflect_structure(engine)

    migrations.downgrade(url, "base")
    remaining = set(inspect(engine).get_table_names())
    assert remaining <= {"alembic_version"}  # everything we created is gone

    migrations.upgrade(url)
    after = reflect_structure(engine)
    assert before == after


def test_schema_version_row_written(tmp_path: Path) -> None:
    url = _url(tmp_path)
    migrations.upgrade(url)
    check = schema_version.verify(get_engine(url))
    assert check.ok
    assert check.found == schema_version.EXPECTED_SCHEMA_VERSION


def test_guard_refuses_uninitialized(tmp_path: Path) -> None:
    engine = get_engine(_url(tmp_path))  # never migrated
    check = schema_version.verify(engine)
    assert check.status == "uninitialized"
    assert not check.ok


def test_guard_refuses_mismatch(tmp_path: Path) -> None:
    url = _url(tmp_path)
    migrations.upgrade(url)
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_version SET version = 999"))
    check = schema_version.verify(engine)
    assert check.status == "mismatch"
    assert check.found == 999
    assert not check.ok


def test_downgrade_with_data_present(tmp_path: Path) -> None:
    """0002 must roll back on a database that holds referencing rows.

    Regression: batch-mode ALTER rebuilds the table, and dropping the old
    ``members`` while ``tokens`` rows reference it trips SQLite's FK
    enforcement. The empty-DB roundtrip cannot catch that, so this one
    migrates, writes a workspace → member → token chain, then walks
    0002 → 0001 → 0002 and expects the rows to survive.
    """
    from sqlmodel import Session

    from kantaq_db.models import Member, Token, Workspace

    url = _url(tmp_path)
    engine = get_engine(url)
    migrations.upgrade(url)

    with Session(engine) as session:
        workspace = Workspace(name="ws")
        session.add(workspace)
        session.flush()
        member = Member(workspace_id=workspace.id, email="a@b.c")
        session.add(member)
        session.flush()
        session.add(Token(member_id=member.id, hashed="$argon2id$placeholder", scopes=[]))
        session.commit()
        member_id = member.id

    migrations.downgrade(url, "-1")
    assert schema_version.verify(engine, expected=1).ok

    migrations.upgrade(url)
    assert schema_version.verify(engine).ok
    with Session(engine) as session:
        survivor = session.get(Member, member_id)
        assert survivor is not None
        assert survivor.status == "active"  # server_default backfilled the row
