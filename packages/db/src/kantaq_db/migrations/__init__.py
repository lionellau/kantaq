"""Programmatic Alembic driver (MOD-02 ``db migrate`` / ``db downgrade``).

We configure Alembic in code rather than via a checked-in ``alembic.ini`` so the
migration scripts travel with the installed package (``script_location`` points
at this directory) and the URL comes from the caller — no path or cwd
assumptions. ``kantaq db migrate`` / ``downgrade`` call straight into here.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from kantaq_db.session import get_engine

_SCRIPT_LOCATION = Path(__file__).resolve().parent


def make_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(make_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(make_config(url), revision)


def current_revision(url: str) -> str | None:
    """The Alembic revision currently stamped on the database (or ``None``)."""
    engine = get_engine(url)
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
