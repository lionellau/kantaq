"""Postgres DDL for the Supabase mirror, generated from the one metadata (D-07).

FR-E24-1 wants Postgres tables 1:1 with the collections, with the migrations in
the repo. The single source of truth stays ``SQLModel.metadata`` (MOD-02): this
module renders that metadata under the Postgres dialect into
``supabase/migrations/0001_collections.sql``, which the maintainer applies to
the Supabase project (E24-T0 is manual; see ``supabase/README.md``).

Two gates keep the checked-in file honest:

- offline (everywhere): ``test_schema_drift`` regenerates the SQL and compares
  it byte-for-byte with the checked-in file, so a model change cannot land
  without regenerating the migration;
- live (CI): ``test_rls`` applies the checked-in file to an EphemeralPostgres
  and compares the reflected structure against a metadata-built schema.

``schema_version`` is deliberately excluded: it is local-replica infrastructure
(FR-E02-4), and Supabase tracks applied migrations itself
(``supabase_migrations.schema_migrations``).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable, Table
from sqlmodel import SQLModel

# Importing the models registers every collection table on SQLModel.metadata.
from kantaq_db.models import COLLECTION_MODELS

# Repo-relative SQL artifact paths (MOD-05 interfaces).
COLLECTIONS_MIGRATION = Path("supabase") / "migrations" / "0001_collections.sql"
POLICIES_FILE = Path("supabase") / "policies" / "0001_rls.sql"
# The sync event log (E24-T4) — hand-written, not generated: it is backend
# infrastructure, not a D-07 collection mirror (MOD-04 "Data").
SYNC_MIGRATION = Path("supabase") / "migrations" / "0002_sync_events.sql"
SYNC_POLICIES_FILE = Path("supabase") / "policies" / "0002_sync_rls.sql"
# The v0.2 atomic commit RPC (E24-T6, D-09) and the append-only trigger
# (E24-T7) — hand-written backend artifacts; apply after the two 0002 files.
EVENTS_RPC = Path("supabase") / "rpc" / "events.sql"
APPEND_ONLY_POLICIES = Path("supabase") / "policies" / "0003_append_only.sql"

_HEADER = """\
-- supabase/migrations/0001_collections.sql
-- GENERATED from SQLModel.metadata (kantaq_db.models) — do not edit by hand.
-- Regenerate: uv run python -m kantaq_backend_supabase.schema
-- The 8 v0.0.5 collections, 1:1 with the local replica (D-07 parity, schema v2).
-- Apply RLS afterwards: supabase/policies/0001_rls.sql (RLS is not optional).
"""


def _collection_tables() -> list[Table]:
    """The 8 collection tables in FK-safe creation order."""
    names = {model.__tablename__ for model in COLLECTION_MODELS}
    return [t for t in SQLModel.metadata.sorted_tables if t.name in names]


def _clean(ddl: str) -> str:
    """Strip the trailing whitespace SQLAlchemy leaves after column commas.

    The repo's pre-commit ``trailing-whitespace`` hook would otherwise rewrite
    the generated file and trip the drift gate.
    """
    return "\n".join(line.rstrip() for line in ddl.strip().splitlines())


def generate_collections_sql() -> str:
    """Render the collections migration deterministically from the metadata."""
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    parts: list[str] = [_HEADER]
    for table in _collection_tables():
        parts.append(f"{_clean(str(CreateTable(table).compile(dialect=dialect)))};")
        for index in sorted(table.indexes, key=lambda ix: ix.name or ""):
            parts.append(f"{_clean(str(CreateIndex(index).compile(dialect=dialect)))};")
    return "\n\n".join(parts) + "\n"


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up to the uv workspace root (the directory holding ``supabase/``)."""
    current = (start or Path(__file__)).resolve()
    for candidate in [current, *current.parents]:
        marker = candidate / "pyproject.toml"
        if marker.is_file() and "tool.uv.workspace" in marker.read_text(encoding="utf-8"):
            return candidate
    raise RuntimeError("uv workspace root not found")


def read_repo_sql(artifact: Path, root: Path | None = None) -> str:
    """Read a checked-in SQL artifact (``COLLECTIONS_MIGRATION`` / ``POLICIES_FILE``)."""
    return (find_repo_root(root) / artifact).read_text(encoding="utf-8")


def write_collections_migration(root: Path | None = None) -> Path:
    """(Re)write the checked-in migration; returns the path written."""
    target = find_repo_root(root) / COLLECTIONS_MIGRATION
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_collections_sql(), encoding="utf-8")
    return target


if __name__ == "__main__":
    print(f"wrote {write_collections_migration()}")
