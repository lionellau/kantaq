"""memory_entries + memory_links for E13 (MOD-19 / MOD-02)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _envelope() -> list[sa.Column[object]]:
    """The CollectionBase envelope, identical to every collection table."""
    return [
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("actor_seq", sa.Integer(), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("hosting_mode", sa.String(length=16), nullable=False),
        sa.Column("retention_policy", sa.String(length=16), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "memory_entries",
        *_envelope(),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("space", sa.String(length=16), nullable=False),
        sa.Column("linked_entities", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "memory_links",
        *_envelope(),
        sa.Column("ticket_id", sa.String(length=26), nullable=False),
        sa.Column("memory_id", sa.String(length=26), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["memory_id"], ["memory_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", "memory_id", name="uq_memory_link_pair"),
    )
    op.create_index(op.f("ix_memory_links_ticket_id"), "memory_links", ["ticket_id"])
    op.create_index(op.f("ix_memory_links_memory_id"), "memory_links", ["memory_id"])

    # Stamp the schema version so the runtime guard (FR-E02-4) sees version 5.
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


def downgrade() -> None:
    op.drop_index(op.f("ix_memory_links_memory_id"), table_name="memory_links")
    op.drop_index(op.f("ix_memory_links_ticket_id"), table_name="memory_links")
    op.drop_table("memory_links")
    op.drop_table("memory_entries")

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
