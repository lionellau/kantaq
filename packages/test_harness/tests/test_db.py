"""The MOD-30 database helpers added for E02."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import text

from kantaq_test_harness.db import POSTGRES_ENV, EphemeralPostgres, temp_sqlite_engine


def test_temp_sqlite_engine_yields_a_working_engine(tmp_path: Path) -> None:
    with temp_sqlite_engine(tmp_path) as engine, engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT 1").scalar() == 1


def test_temp_sqlite_fixture_is_provided(temp_sqlite: object) -> None:
    # The autoloaded pytest11 plugin exposes `temp_sqlite`.
    with temp_sqlite.connect() as conn:  # type: ignore[attr-defined]
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_ephemeral_postgres_available_reflects_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(POSTGRES_ENV, raising=False)
    assert EphemeralPostgres.available() is False
    monkeypatch.setenv(POSTGRES_ENV, "postgresql://user:pw@localhost:5432/postgres")
    assert EphemeralPostgres.available() is True


def test_ephemeral_postgres_normalizes_driver(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(POSTGRES_ENV, "postgresql://user:pw@localhost:5432/postgres")
    pg = EphemeralPostgres()
    assert pg._base_url.startswith("postgresql+psycopg://")  # noqa: SLF001


def test_ephemeral_postgres_requires_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(POSTGRES_ENV, raising=False)
    raised = False
    try:
        EphemeralPostgres()
    except RuntimeError:
        raised = True
    assert raised


# Real round-trip — only when a server is configured (CI service container).
if os.environ.get(POSTGRES_ENV):

    def test_ephemeral_postgres_create_and_drop() -> None:
        with EphemeralPostgres() as engine, engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
