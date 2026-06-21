"""Schema-version guard (FR-E02-4).

The migration writes one row into ``schema_version``. The code knows the version
it was built for (``EXPECTED_SCHEMA_VERSION``). On boot the runtime calls
``verify`` and refuses to start unless they match, so a stale binary never reads
or writes a schema it does not understand.

Statuses:
- ``ok`` — the DB is at the expected version.
- ``uninitialized`` — no ``schema_version`` row yet (run ``kantaq db migrate``).
- ``mismatch`` — the DB is at a different version than the code expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Bump this whenever a migration changes the version row. Version 17 adds the
# E15 v0.3 follow-up slice (MOD-29): the ``follow_ups`` collection — one new
# syncable collection (20 declared / 15 on the backend sync allowlist now).
# Version 16 added the E14 milestone slice (``milestones`` + ``ticket_milestones``).
EXPECTED_SCHEMA_VERSION = 17
# The Alembic head revision that defines the expected schema. Kept in sync with
# the migration filename in ``migrations/versions``.
HEAD_REVISION = "0017"

Status = Literal["ok", "uninitialized", "mismatch"]


@dataclass(frozen=True)
class SchemaCheck:
    status: Status
    expected: int
    found: int | None
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def verify(engine: Engine, *, expected: int = EXPECTED_SCHEMA_VERSION) -> SchemaCheck:
    """Compare the DB's recorded schema version against ``expected``."""
    inspector = inspect(engine)
    if not inspector.has_table("schema_version"):
        return SchemaCheck(
            "uninitialized",
            expected,
            None,
            "database schema is not initialized; run `kantaq db migrate`",
        )
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        ).first()
    found = int(row[0]) if row is not None else None
    if found is None:
        return SchemaCheck(
            "uninitialized",
            expected,
            None,
            "schema_version table is empty; run `kantaq db migrate`",
        )
    if found != expected:
        return SchemaCheck(
            "mismatch",
            expected,
            found,
            (
                f"schema version mismatch: database is at {found}, this build "
                f"expects {expected}; run `kantaq db migrate`"
            ),
        )
    return SchemaCheck("ok", expected, found, f"schema version {found} matches")
