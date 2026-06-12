"""ticket attachment refs for E12-T2: tickets.attachments

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN (same reasoning as 0002: batch mode would rebuild the
    # table under FK enforcement). JSON list of blob refs; server_default "[]"
    # backfills pre-E12 rows, matching the model's default_factory=list.
    op.add_column(
        "tickets",
        sa.Column("attachments", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )

    # Stamp the schema version so the runtime guard (FR-E02-4) sees version 3.
    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 3, "revision": "0003", "applied_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.drop_column("tickets", "attachments")

    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 2, "revision": "0002", "applied_at": datetime.now(UTC)}],
    )
