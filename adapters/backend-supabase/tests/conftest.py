"""Shared fixtures for the Postgres-gated sync tests (E24-T4, Backend profile).

A disposable Postgres with the Supabase auth environment stubbed and the four
checked-in SQL artifacts applied — the same files the maintainer applies to
the real project — plus the two-workspace seed the RLS suite attacks.
Opt-in via ``KANTAQ_TEST_POSTGRES_URL`` (the CI Postgres service provides it).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from kantaq_backend_supabase.schema import (
    COLLECTIONS_MIGRATION,
    POLICIES_FILE,
    SYNC_MIGRATION,
    SYNC_POLICIES_FILE,
    read_repo_sql,
)
from kantaq_test_harness.db import EphemeralPostgres
from kantaq_test_harness.rls import apply_sql, install_supabase_auth_stub

ENVELOPE = "now(), now(), 0, 'team', 'plain', 'standard'"

# The two-replica simulator's actor ids (replica._build's naming), mirrored
# into the members table — the v0.0.5 "team manifest" baseline.
ACTOR_A = f"mbr_{'a'.ljust(22, '0')}"
ACTOR_B = f"mbr_{'b'.ljust(22, '0')}"

# Two workspaces; sync RLS is attacked from workspace A. The shared workspace
# row + member rows for the two-replica simulator (replica.WORKSPACE_ID and
# its mbr_<name> actor ids) are the v0.0.5 "team manifest" baseline.
SYNC_SEED = f"""
insert into workspaces (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, name) values
  ('ws_a', {ENVELOPE}, 'Acme'),
  ('ws_b', {ENVELOPE}, 'Other'),
  ('ws_shared0000000000000000', {ENVELOPE}, 'Shared Workspace');

insert into members (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, workspace_id, email, role, status) values
  ('mbr_alice', {ENVELOPE}, 'ws_a', 'alice@acme.dev', 'Owner', 'active'),
  ('mbr_bob',   {ENVELOPE}, 'ws_a', 'bob@acme.dev',   'Member', 'active'),
  ('mbr_rev',   {ENVELOPE}, 'ws_a', 'rev@acme.dev',   'Member', 'revoked'),
  ('mbr_cher',  {ENVELOPE}, 'ws_b', 'cher@other.dev', 'Owner', 'active'),
  ('{ACTOR_A}', {ENVELOPE}, 'ws_shared0000000000000000', 'a@team.dev', 'Owner', 'active'),
  ('{ACTOR_B}', {ENVELOPE}, 'ws_shared0000000000000000', 'b@team.dev', 'Member', 'active');

insert into sync_events (event_id, collection, entity_id, actor_id, actor_seq, op,
  payload, workspace_id) values
  ('evt_seed_a0000000000000000', 'tickets', 'tkt_a', 'mbr_alice', 1, 'patch',
   '{{"title": "A ticket"}}'::json, 'ws_a'),
  ('evt_seed_b0000000000000000', 'tickets', 'tkt_b', 'mbr_cher', 1, 'patch',
   '{{"title": "B secret"}}'::json, 'ws_b');
"""


@pytest.fixture
def sync_pg() -> Iterator[Engine]:
    """Disposable Postgres: auth stub + the checked-in artifacts + the seed."""
    if not EphemeralPostgres.available():
        pytest.skip("no KANTAQ_TEST_POSTGRES_URL (the CI Postgres service provides one)")
    with EphemeralPostgres() as engine:
        install_supabase_auth_stub(engine)
        apply_sql(engine, read_repo_sql(COLLECTIONS_MIGRATION))
        apply_sql(engine, read_repo_sql(POLICIES_FILE))
        apply_sql(engine, read_repo_sql(SYNC_MIGRATION))
        apply_sql(engine, read_repo_sql(SYNC_POLICIES_FILE))
        apply_sql(engine, SYNC_SEED)
        yield engine
