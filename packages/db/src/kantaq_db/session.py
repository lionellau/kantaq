"""Engine + session factory (MOD-02 ``db.session``).

The local replica is SQLite in **WAL** mode (FR-E02-6): WAL lets the runtime
read while a write is in flight, which matters once sync and the UI poll the same
file. We enable it (and foreign-key enforcement, off by default in SQLite) on
every new connection via a connect listener. Postgres needs neither.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

DEFAULT_SQLITE_PATH = "./data/local.sqlite"


def sqlite_url(db_path: str | os.PathLike[str], *, create_parent: bool = True) -> str:
    """Build a SQLite URL from a filesystem path, creating its parent dir."""
    path = Path(db_path).expanduser()
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def default_db_url() -> str:
    """Resolve the DB URL from ``KANTAQ_DB_URL`` or the default local SQLite path."""
    env = os.environ.get("KANTAQ_DB_URL")
    if env:
        return env
    return sqlite_url(DEFAULT_SQLITE_PATH)


def _enable_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an engine for ``url`` (default: resolved local SQLite), WAL on."""
    resolved = url or default_db_url()
    engine = create_engine(resolved, echo=echo)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def get_session(engine: Engine) -> Session:
    """A new SQLModel session bound to ``engine``."""
    return Session(engine)
