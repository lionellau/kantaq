"""Fixtures for the self-hosted Postgres backend tests (E25-T1, Backend profile).

A disposable Postgres with the self-hosted schema created (the ORM trust/mirror
tables + the ``sync_events`` log) and one seeded workspace — the FK target every
committed event needs. Opt-in via ``KANTAQ_TEST_POSTGRES_URL`` (the CI Postgres
service provides it); skipped cleanly where no server is configured, exactly
like the Supabase backend suite.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from kantaq_backend_postgres import PostgresSyncBackend, create_schema
from kantaq_test_harness.db import EphemeralPostgres

WORKSPACE_ID = "ws_a"

# The seed envelope mirrors the Supabase suite (the schema is the same ORM, D-07):
# (created_at, updated_at, actor_seq, visibility, hosting_mode, retention_policy).
_ENVELOPE = "now(), now(), 0, 'team', 'plain', 'standard'"

_SEED = f"""
insert into workspaces (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, name) values
  ('{WORKSPACE_ID}', {_ENVELOPE}, 'Acme');
"""


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """A disposable Postgres with the self-hosted schema + one workspace seeded."""
    if not EphemeralPostgres.available():
        pytest.skip("no KANTAQ_TEST_POSTGRES_URL (the CI Postgres service provides one)")
    with EphemeralPostgres() as engine:
        create_schema(engine)
        with engine.begin() as conn:
            conn.execute(text(_SEED))
        yield engine


@pytest.fixture
def pg_backend(pg_engine: Engine) -> PostgresSyncBackend:
    """The backend scoped to the seeded workspace."""
    return PostgresSyncBackend(pg_engine, workspace_id=WORKSPACE_ID)
