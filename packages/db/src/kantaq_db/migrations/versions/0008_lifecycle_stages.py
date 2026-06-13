"""normalize tickets.lifecycle_stage to the locked taxonomy (MOD-20 / E14)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-13

Data-only. v0.0.5 accepted any slug for ``lifecycle_stage``; E14 locks the
9-stage taxonomy and the tracker now validates membership, so rows written
before the lock are normalized to the entry stage (``intake``). One-way: the
downgrade restores the schema_version row, not the pre-taxonomy slugs (there
is nothing to restore them from, and ``intake`` is the v0.0.5 default).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The MOD-20 taxonomy, inlined: a migration's meaning must never change when
# code does (kantaq_core.lifecycle.STAGE_SLUGS is pinned to the same set).
_STAGE_SLUGS = (
    "intake",
    "discovery",
    "planning",
    "design",
    "implementation",
    "review",
    "qa",
    "release",
    "learn",
)


def _write_version(version: int, rev: str) -> None:
    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": version, "revision": rev, "applied_at": datetime.now(UTC)}],
    )


def upgrade() -> None:
    tickets = sa.table("tickets", sa.column("lifecycle_stage", sa.String))
    op.execute(
        tickets.update()
        .where(tickets.c.lifecycle_stage.notin_(_STAGE_SLUGS))
        .values(lifecycle_stage="intake")
    )
    _write_version(8, "0008")


def downgrade() -> None:
    _write_version(7, "0007")
