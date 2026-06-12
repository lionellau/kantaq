"""telemetry tables for E28: telemetry_events, local_settings

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telemetry_events",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=26), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("props", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_telemetry_events_name"), "telemetry_events", ["name"], unique=False)

    op.create_table(
        "local_settings",
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("value", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 6, "revision": "0006", "applied_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_telemetry_events_name"), table_name="telemetry_events")
    op.drop_table("telemetry_events")
    op.drop_table("local_settings")

    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 5, "revision": "0005", "applied_at": datetime.now(UTC)}],
    )
