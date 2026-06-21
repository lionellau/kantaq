"""milestones + ticket_milestones for E14-T2 (MOD-20 v0.3 / MOD-02)

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-21

Adds the v0.3 milestone slice (FR-E14-3): a flat ``milestones`` entity scoped to
a project (name, description, optional target_date, status active/complete/
archived) and the ``ticket_milestones`` junction that groups a project's tickets
under a milestone. The UNIQUE backs the no-duplicate-membership rule at the
database for the exact pair; the same-project integrity rule lives in the one
write path (kantaq_core.tracker). Both tables carry the standard CollectionBase
envelope and sync lww like the other tracker collections.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
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
        "milestones",
        *_envelope(),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("target_date", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_milestones_project_id"), "milestones", ["project_id"])

    op.create_table(
        "ticket_milestones",
        *_envelope(),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("milestone_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", "milestone_id", name="uq_ticket_milestone"),
    )
    op.create_index(op.f("ix_ticket_milestones_ticket_id"), "ticket_milestones", ["ticket_id"])
    op.create_index(
        op.f("ix_ticket_milestones_milestone_id"), "ticket_milestones", ["milestone_id"]
    )

    _write_version(16, "0016")


def downgrade() -> None:
    op.drop_index(op.f("ix_ticket_milestones_milestone_id"), table_name="ticket_milestones")
    op.drop_index(op.f("ix_ticket_milestones_ticket_id"), table_name="ticket_milestones")
    op.drop_table("ticket_milestones")

    op.drop_index(op.f("ix_milestones_project_id"), table_name="milestones")
    op.drop_table("milestones")

    _write_version(15, "0015")
