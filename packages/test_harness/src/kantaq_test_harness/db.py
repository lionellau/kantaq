"""Database test doubles for the Domain/migration profile (MOD-30).

Two helpers the data layer (MOD-02) and later the backends (MOD-05/MOD-28) lean
on:

- ``temp_sqlite_engine`` — a throwaway file-backed SQLite engine (file-backed so
  WAL behaves like production), torn down with the test.
- ``EphemeralPostgres`` — a disposable Postgres database created on a server given
  by ``KANTAQ_TEST_POSTGRES_URL`` and dropped on exit. It is *opt-in*: when the
  env var is unset (local dev, the fresh-clone job) ``available()`` is False and
  tests that need a real Postgres skip with a clear reason. CI provides the server
  via a Postgres service container, so the live checks actually run there.

Neither helper imports the ORM, so the harness stays a leaf dependency.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from secrets import token_hex

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.engine import create_engine as _create_engine

POSTGRES_ENV = "KANTAQ_TEST_POSTGRES_URL"


@contextmanager
def temp_sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a file-backed SQLite engine under ``tmp_path``, disposed on exit."""
    engine = _create_engine(f"sqlite:///{tmp_path / 'harness.sqlite'}")
    try:
        yield engine
    finally:
        engine.dispose()


def _normalize_driver(url: str) -> str:
    """Force the psycopg (v3) driver so a bare ``postgresql://`` URL still works."""
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


class EphemeralPostgres:
    """A disposable Postgres database for one test.

    Usage::

        if not EphemeralPostgres.available():
            pytest.skip("no KANTAQ_TEST_POSTGRES_URL")
        with EphemeralPostgres() as engine:
            SQLModel.metadata.create_all(engine)
    """

    def __init__(self, base_url: str | None = None) -> None:
        raw = base_url or os.environ.get(POSTGRES_ENV)
        if not raw:
            raise RuntimeError(f"{POSTGRES_ENV} is not set; check available() first")
        self._base_url = _normalize_driver(raw)
        self._db_name = f"kantaq_test_{token_hex(8)}"
        self._engine: Engine | None = None

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get(POSTGRES_ENV))

    def __enter__(self) -> Engine:
        admin = _create_engine(self._base_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{self._db_name}"'))
        finally:
            admin.dispose()
        target = make_url(self._base_url).set(database=self._db_name)
        self._engine = _create_engine(target)
        return self._engine

    def __exit__(self, *_exc: object) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        admin = _create_engine(self._base_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                # Terminate stragglers so DROP DATABASE isn't blocked.
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": self._db_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{self._db_name}"'))
        finally:
            admin.dispose()
