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
    "ticket_relationships",
    "members",
    "tokens",
    "audit_events",
    "agent_proposals",
    "schema_version",
    "event_log",
    "sync_cursors",
    "memory_entries",
    "memory_links",
    "telemetry_events",
    "local_settings",
    "devices",
    "capability_grants",
    "skill_containers",
    "skill_mappings",
    "conflict_records",
    "milestones",
    "ticket_milestones",
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


def _string_lengths(columns: object) -> dict[str, int | None]:
    # Character columns expose ``.length`` (String/VARCHAR and SQLModel's
    # AutoString — which is NOT a sqlalchemy.String subclass, so isinstance would
    # silently skip every model column); Integer/DateTime/JSON do not.
    out: dict[str, int | None] = {}
    for col in columns:  # type: ignore[attr-defined]
        coltype = col.type if hasattr(col, "type") else col["type"]
        name = col.name if hasattr(col, "name") else col["name"]
        if hasattr(coltype, "length"):
            out[name] = coltype.length
    return out


def test_migration_string_lengths_match_models(tmp_path: Path) -> None:
    """VARCHAR length parity model vs migration (D-07).

    Alembic's ``compare_type`` is length-blind on SQLite (VARCHAR(26) == VARCHAR),
    so ``test_migration_matches_models`` misses a hand-written migration that
    bounds a column the model leaves unbounded (or vice versa). SQLite *reflection*
    does preserve the declared length, so we compare it directly: the one model
    definition (D-07) must render the same column length the migration builds.
    """
    url = _url(tmp_path)
    migrations.upgrade(url)
    inspector = inspect(get_engine(url))

    model_lengths: dict[tuple[str, str], int | None] = {}
    for table in SQLModel.metadata.sorted_tables:
        for name, length in _string_lengths(table.columns).items():
            model_lengths[(table.name, name)] = length

    drifts: list[tuple[str, str, int | None, int | None]] = []
    for table_name in inspector.get_table_names():
        built = _string_lengths(inspector.get_columns(table_name))
        for name, built_length in built.items():
            key = (table_name, name)
            if key in model_lengths and model_lengths[key] != built_length:
                drifts.append((table_name, name, model_lengths[key], built_length))

    assert drifts == [], f"VARCHAR length drift (model_len, migration_len): {sorted(drifts)}"


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
    """Every revision must roll back on a database that holds referencing rows.

    Regression (0002): batch-mode ALTER rebuilds the table, and dropping the
    old ``members`` while ``tokens`` rows reference it trips SQLite's FK
    enforcement. The empty-DB roundtrip cannot catch that, so this one
    migrates, writes a workspace → member → token chain, then walks
    head → 0001 → head and expects the rows to survive.
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

    migrations.downgrade(url, "0001")
    assert schema_version.verify(engine, expected=1).ok

    migrations.upgrade(url)
    assert schema_version.verify(engine).ok
    with Session(engine) as session:
        survivor = session.get(Member, member_id)
        assert survivor is not None
        assert survivor.status == "active"  # server_default backfilled the row


def test_lifecycle_stage_normalized_to_taxonomy(tmp_path: Path) -> None:
    """0008 rewrites pre-taxonomy stage slugs to ``intake`` (MOD-20 / E14).

    Data-only and one-way: rows written under v0.0.5's any-slug rule join the
    locked taxonomy; canonical slugs are untouched; the downgrade restores the
    version row (the old slugs are gone by design).
    """
    from sqlmodel import Session

    from kantaq_db.models import Project, Ticket, Workspace

    url = _url(tmp_path)
    engine = get_engine(url)
    migrations.upgrade(url, "0007")  # the schema before the taxonomy lock

    with Session(engine) as session:
        workspace = Workspace(name="ws")
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, name="p")
        session.add(project)
        session.flush()
        legacy = Ticket(project_id=project.id, title="legacy", lifecycle_stage="build")
        canonical = Ticket(project_id=project.id, title="canonical", lifecycle_stage="qa")
        session.add(legacy)
        session.add(canonical)
        session.commit()
        legacy_id, canonical_id = legacy.id, canonical.id

    migrations.upgrade(url)
    assert schema_version.verify(engine).ok
    with Session(engine) as session:
        normalized = session.get(Ticket, legacy_id)
        untouched = session.get(Ticket, canonical_id)
        assert normalized is not None and normalized.lifecycle_stage == "intake"
        assert untouched is not None and untouched.lifecycle_stage == "qa"

    migrations.downgrade(url, "0007")
    assert schema_version.verify(engine, expected=7).ok
    migrations.upgrade(url)
    assert schema_version.verify(engine).ok


def test_skill_containers_seed_on_upgrade_and_roll_back(tmp_path: Path) -> None:
    """0010 seeds the 29 hardcoded containers and rolls back cleanly (E17-T4).

    The migration moves the v0.1 hardcoded registry behind a db table: the seed
    is static literals (no kantaq_core import), so the row count and a sample
    row's JSON-list columns are pinned here. Down to 0009 drops both tables and
    restores the version row; up again re-seeds the same 29 rows.
    """
    from sqlmodel import Session, select

    from kantaq_db.models import SkillContainerRow

    url = _url(tmp_path)
    engine = get_engine(url)
    migrations.upgrade(url)

    with Session(engine) as session:
        rows = session.exec(select(SkillContainerRow)).all()
        assert len(rows) == 29
        triage = {r.slug: r for r in rows}["triage"]
        assert triage.recommended_roles == ["product_agent"]
        assert triage.supported_stages == ["intake"]
        assert triage.default_write_mode == "read"
        assert triage.risk_level == "low"
        assert "role_context_get" in triage.allowed_tools

    migrations.downgrade(url, "0009")
    assert schema_version.verify(engine, expected=9).ok
    remaining = set(inspect(engine).get_table_names())
    assert "skill_containers" not in remaining
    assert "skill_mappings" not in remaining

    migrations.upgrade(url)
    assert schema_version.verify(engine).ok
    with Session(engine) as session:
        assert len(session.exec(select(SkillContainerRow)).all()) == 29
