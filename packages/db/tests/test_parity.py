"""SQLite/Postgres dialect parity (E02-T3, D-07, NFR-E02-1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import SQLModel

from kantaq_db.parity import (
    check_parity,
    compile_create,
    dialect_structure,
    reflect_structure,
)
from kantaq_db.session import get_engine, sqlite_url
from kantaq_test_harness.db import EphemeralPostgres


def test_offline_parity_holds() -> None:
    ok, message = check_parity()
    assert ok, message


def test_both_dialects_render_every_table() -> None:
    sqlite_ddl = compile_create("sqlite")
    postgres_ddl = compile_create("postgresql")
    assert set(sqlite_ddl) == set(postgres_ddl)
    # 12 collections (8 v0.0.5 + the E13 memory pair + the E06 identity
    # pair) + the local infrastructure: schema_version (E02), event_log +
    # sync_cursors (E04), telemetry_events + local_settings (E28).
    assert len(sqlite_ddl) == 17
    assert all(ddl.strip().upper().startswith("CREATE TABLE") for ddl in postgres_ddl.values())


def test_dialect_structures_are_identical() -> None:
    assert dialect_structure("sqlite") == dialect_structure("postgresql")


@pytest.mark.skipif(
    not EphemeralPostgres.available(),
    reason="set KANTAQ_TEST_POSTGRES_URL to run the live Postgres parity check",
)
def test_live_sqlite_postgres_parity(tmp_path: Path) -> None:
    sqlite_engine = get_engine(sqlite_url(tmp_path / "parity.sqlite"))
    SQLModel.metadata.create_all(sqlite_engine)
    sqlite_struct = reflect_structure(sqlite_engine)

    with EphemeralPostgres() as pg_engine:
        SQLModel.metadata.create_all(pg_engine)
        postgres_struct = reflect_structure(pg_engine)

    assert sqlite_struct == postgres_struct
