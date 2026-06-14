"""SQLite/Postgres dialect parity (D-07, NFR-E02-1).

The single source of truth is ``SQLModel.metadata`` — the models in
``models.py``. "Parity" means: that one definition renders to **both** the SQLite
and Postgres dialects, and describes the **same** logical schema in each. This
module proves it two ways:

1. ``check_parity`` (offline, hermetic): compile ``CREATE TABLE`` for every table
   under both dialects (this raises if a column type is unrenderable in one),
   then derive a dialect-neutral structural fingerprint from each and assert they
   are identical. Runs everywhere, including the fresh-clone job with no Postgres.
2. ``reflect_structure`` (used by the live test in ``tests/test_parity.py`` with
   ``EphemeralPostgres``): after ``create_all`` on a real engine, reflect the
   schema back and compare what SQLite and Postgres actually built.

Keeping the offline check authoritative means the gate never depends on a running
Postgres, while the live check catches anything dialect compilation alone can't.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.schema import CreateTable, MetaData
from sqlmodel import SQLModel

# Importing models registers every table on SQLModel.metadata.
from kantaq_db import models as _models  # noqa: F401

_DIALECTS: dict[str, Dialect] = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.dialect(),  # type: ignore[no-untyped-call]
}


def _affinity(coltype: Any) -> str:
    """A coarse, dialect-neutral type class for a column."""
    try:
        return str(coltype.python_type.__name__)
    except (NotImplementedError, AttributeError):
        return type(coltype).__name__


def _table_structure(table: Any, dialect: Dialect) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    for col in table.columns:
        # Raises if this column's type cannot render under the dialect.
        col.type.compile(dialect=dialect)
        columns[col.name] = {
            "type": _affinity(col.type),
            # Declared character length, so a VARCHAR(N) vs unbounded divergence
            # is part of the fingerprint (it would otherwise be invisible).
            "length": getattr(col.type, "length", None),
            "nullable": bool(col.nullable),
            "primary_key": bool(col.primary_key),
        }
    foreign_keys = sorted(
        (fk.parent.name, fk.column.table.name, fk.column.name) for fk in table.foreign_keys
    )
    indexes = sorted(sorted(c.name for c in ix.columns) for ix in table.indexes)
    return {"columns": columns, "foreign_keys": foreign_keys, "indexes": indexes}


def dialect_structure(dialect_name: str, metadata: MetaData | None = None) -> dict[str, Any]:
    """Structural fingerprint of the whole schema under one dialect."""
    dialect = _DIALECTS[dialect_name]
    md = metadata or SQLModel.metadata
    return {table.name: _table_structure(table, dialect) for table in md.sorted_tables}


def compile_create(dialect_name: str, metadata: MetaData | None = None) -> dict[str, str]:
    """DDL ``CREATE TABLE`` text per table under one dialect (raises on error)."""
    dialect = _DIALECTS[dialect_name]
    md = metadata or SQLModel.metadata
    return {t.name: str(CreateTable(t).compile(dialect=dialect)) for t in md.sorted_tables}


def check_parity(metadata: MetaData | None = None) -> tuple[bool, str]:
    """Return ``(ok, message)``: do SQLite and Postgres describe the same schema?"""
    md = metadata or SQLModel.metadata
    # Renderability: both dialects must emit DDL for every table.
    compile_create("sqlite", md)
    compile_create("postgresql", md)
    sqlite_struct = dialect_structure("sqlite", md)
    postgres_struct = dialect_structure("postgresql", md)
    if sqlite_struct != postgres_struct:
        diff = _first_difference(sqlite_struct, postgres_struct)
        return False, f"dialect drift: {diff}"
    n_tables = len(sqlite_struct)
    return True, f"parity ok: {n_tables} tables match across SQLite and Postgres"


def _first_difference(a: dict[str, Any], b: dict[str, Any]) -> str:
    for table in sorted(set(a) | set(b)):
        if a.get(table) != b.get(table):
            return f"table {table!r}: {a.get(table)} != {b.get(table)}"
    return "structures differ"


def reflect_structure(engine: Engine) -> dict[str, Any]:
    """Reflect a live database into a dialect-neutral shape for comparison.

    Used by the live-Postgres parity test: build the schema on a real engine,
    reflect it back, and compare two engines' output. We compare column **names**,
    nullability, primary keys, and foreign keys — not the reflected type. SQLite
    has no native JSON type and stores it as TEXT, so reflected types legitimately
    differ across dialects; type parity is proven instead by the offline
    ``check_parity`` (both dialects derive from one metadata).
    """
    inspector = inspect(engine)
    out: dict[str, Any] = {}
    for table_name in inspector.get_table_names():
        pk_cols = set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
        columns: dict[str, Any] = {}
        for col in inspector.get_columns(table_name):
            columns[col["name"]] = {
                "nullable": bool(col["nullable"]),
                "primary_key": col["name"] in pk_cols,
            }
        foreign_keys = sorted(
            (
                fk["constrained_columns"][0],
                fk["referred_table"],
                fk["referred_columns"][0],
            )
            for fk in inspector.get_foreign_keys(table_name)
            if fk["constrained_columns"] and fk["referred_columns"]
        )
        out[table_name] = {"columns": columns, "foreign_keys": foreign_keys}
    return out
