"""identity columns for E06: members.status, tokens.revoked_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD/DROP COLUMN (no batch table-recreate): batch mode rebuilds the
    # table, and dropping the old `members` while `tokens` rows reference it
    # trips SQLite's FK enforcement (ON for every connection — session.py).
    # SQLite supports both natively for plain, unindexed columns like these.
    # server_default backfills pre-E06 rows; the model default is "active".
    op.add_column(
        "members",
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column("tokens", sa.Column("revoked_at", sa.DateTime(), nullable=True))

    # Stamp the schema version so the runtime guard (FR-E02-4) sees version 2.
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


def downgrade() -> None:
    op.drop_column("tokens", "revoked_at")
    op.drop_column("members", "status")

    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 1, "revision": "0001", "applied_at": datetime.now(UTC)}],
    )
