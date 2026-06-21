"""notification_deadletter for E20-T8 (MOD-12 / PRD §16.10)

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-21

Adds ``notification_deadletter`` — where an outbound notification that failed
every retry lands so it is not lost (an operator signal: "your sink is down").
Local infrastructure like ``telemetry_events`` / ``local_settings``: NOT a
syncable collection (absent from COLLECTION_META), so a failed signal never
leaves the machine via sync. ``payload`` is the content-free
``{action, ids, actor, deep_link}`` that was attempted — no ticket/memory body.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
    op.create_table(
        "notification_deadletter",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("sink_type", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    _write_version(18, "0018")


def downgrade() -> None:
    op.drop_table("notification_deadletter")

    _write_version(17, "0017")
