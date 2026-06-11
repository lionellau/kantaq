"""Alembic environment for kantaq (MOD-02).

Online migrations only (the runtime always has a live engine). ``target_metadata``
is the SQLModel metadata, so ``--autogenerate`` and ``compare_metadata`` diff
against the models in ``kantaq_db.models``. ``render_as_batch`` is on so SQLite
can run table-rebuild ALTERs in later migrations.
"""

from __future__ import annotations

from alembic import context
from sqlmodel import SQLModel

import kantaq_db.models  # noqa: F401  (registers every table on the metadata)
from kantaq_db.session import get_engine

target_metadata = SQLModel.metadata


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    assert url is not None, "sqlalchemy.url must be set on the Alembic config"
    engine = get_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
