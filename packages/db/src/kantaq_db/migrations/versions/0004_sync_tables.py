"""sync engine tables for E04: event_log, sync_cursors

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("event_id", sqlmodel.sql.sqltypes.AutoString(length=26), nullable=False),
        sa.Column("collection", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("entity_id", sqlmodel.sql.sqltypes.AutoString(length=26), nullable=False),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(length=26), nullable=False),
        sa.Column("actor_seq", sa.Integer(), nullable=False),
        sa.Column("op", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("base_rev", sa.Integer(), nullable=True),
        sa.Column("policy_ref", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("sig", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("committed_rev", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("actor_id", "actor_seq", name="uq_event_actor_seq"),
    )
    op.create_index(op.f("ix_event_log_collection"), "event_log", ["collection"], unique=False)
    op.create_index(op.f("ix_event_log_entity_id"), "event_log", ["entity_id"], unique=False)
    op.create_index(
        op.f("ix_event_log_committed_rev"), "event_log", ["committed_rev"], unique=False
    )

    op.create_table(
        "sync_cursors",
        sa.Column("collection", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(length=26), nullable=False),
        sa.Column("acked_rev", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("collection", "actor_id"),
    )

    # Stamp the schema version so the runtime guard (FR-E02-4) sees version 4.
    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 4, "revision": "0004", "applied_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_event_log_committed_rev"), table_name="event_log")
    op.drop_index(op.f("ix_event_log_entity_id"), table_name="event_log")
    op.drop_index(op.f("ix_event_log_collection"), table_name="event_log")
    op.drop_table("sync_cursors")
    op.drop_table("event_log")

    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("applied_at", sa.DateTime),
            sa.column("revision", sa.String),
        ),
        [{"version": 3, "revision": "0003", "applied_at": datetime.now(UTC)}],
    )
