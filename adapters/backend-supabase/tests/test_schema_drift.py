"""Offline gates for the checked-in SQL artifacts (E24-T1, D-07).

Hermetic — no Postgres. The live half (apply the file, reflect, compare) is in
``test_rls.py`` and runs against the CI Postgres service.
"""

from __future__ import annotations

from kantaq_backend_supabase.schema import (
    COLLECTIONS_MIGRATION,
    POLICIES_FILE,
    generate_collections_sql,
    read_repo_sql,
)
from kantaq_db.models import COLLECTION_MODELS

COLLECTION_TABLES = [model.__tablename__ for model in COLLECTION_MODELS]


def test_checked_in_migration_matches_the_models() -> None:
    """A model change cannot land without regenerating the migration."""
    checked_in = read_repo_sql(COLLECTIONS_MIGRATION)
    assert checked_in == generate_collections_sql(), (
        "supabase/migrations/0001_collections.sql is stale — regenerate with "
        "`uv run python -m kantaq_backend_supabase.schema`"
    )


def test_migration_covers_all_collections_1_to_1() -> None:
    sql = read_repo_sql(COLLECTIONS_MIGRATION)
    for table in COLLECTION_TABLES:
        assert f"CREATE TABLE {table} (" in sql
    # 1:1 means exactly the 8 collections, nothing else.
    assert sql.count("CREATE TABLE") == len(COLLECTION_TABLES)


def test_local_infrastructure_is_not_mirrored() -> None:
    """schema_version is local-replica bootkeeping, not a synced collection."""
    assert "schema_version" not in read_repo_sql(COLLECTIONS_MIGRATION)


def test_every_collection_has_rls_enabled_in_the_policies() -> None:
    """No table ships without RLS — a missing line here is a missing wall."""
    policies = read_repo_sql(POLICIES_FILE)
    for table in COLLECTION_TABLES:
        assert f"alter table {table}" in policies and "enable row level security" in policies, (
            f"{table} has no RLS enablement in supabase/policies/0001_rls.sql"
        )
        assert f"on {table}" in policies, f"{table} has no policy in 0001_rls.sql"


def test_audit_events_have_no_update_or_delete_policy() -> None:
    """Append-only at the database: only select/insert policies exist."""
    policies = read_repo_sql(POLICIES_FILE)
    for verb in ("update", "delete"):
        assert f"create policy audit_events_{verb}" not in policies
    assert "create policy audit_events_select" in policies
    assert "create policy audit_events_insert" in policies


def test_service_role_key_never_in_sql_artifacts() -> None:
    """The SQL in the repo references the role, never any key material."""
    for artifact in (COLLECTIONS_MIGRATION, POLICIES_FILE):
        content = read_repo_sql(artifact).lower()
        assert "service_role_key" not in content
        assert "eyj" not in content  # base64url JWT header prefix


def test_generated_sql_survives_the_trailing_whitespace_hook() -> None:
    """pre-commit strips trailing whitespace; the generator must emit none."""
    assert not [line for line in generate_collections_sql().splitlines() if line != line.rstrip()]
