"""ticket_relationships for E12-T3 (MOD-03 v0.1 / MOD-02)

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-13

Adds the typed ticket-relationship collection (FR-E12-3): a directed edge
(from_id, to_id, type) between two tickets. The UNIQUE backs the no-duplicate
rule at the database for the exact spelling; the symmetric/inverse collapse and
the no-self/no-cycle rules live in the one write path (kantaq_core.tracker).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
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
        "ticket_relationships",
        *_envelope(),
        sa.Column("from_id", sa.String(length=26), nullable=False),
        sa.Column("to_id", sa.String(length=26), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["from_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["to_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_id", "to_id", "type", name="uq_ticket_relationship"),
    )
    op.create_index(op.f("ix_ticket_relationships_from_id"), "ticket_relationships", ["from_id"])
    op.create_index(op.f("ix_ticket_relationships_to_id"), "ticket_relationships", ["to_id"])

    _write_version(9, "0009")


def downgrade() -> None:
    op.drop_index(op.f("ix_ticket_relationships_to_id"), table_name="ticket_relationships")
    op.drop_index(op.f("ix_ticket_relationships_from_id"), table_name="ticket_relationships")
    op.drop_table("ticket_relationships")

    _write_version(8, "0008")
