"""follow_ups for E15-T1 (MOD-29 v0.3 / MOD-02)

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-21

Adds the v0.3 follow-up slice (FR-E15-1): a ``follow_ups`` collection — a
self-scheduled reminder an agent (or human) attaches to a ticket (title, optional
body, status open/done/dismissed, optional due_at, provenance). Agent-created
follow-ups are propose-first (the proposal lands in ``agent_proposals``; the
follow_up row is written only on human approval), so the row itself is always a
human-committed tracker write. Carries the standard CollectionBase envelope and
syncs lww like the other tracker collections.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
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
        "follow_ups",
        *_envelope(),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_follow_ups_ticket_id"), "follow_ups", ["ticket_id"])

    _write_version(17, "0017")


def downgrade() -> None:
    op.drop_index(op.f("ix_follow_ups_ticket_id"), table_name="follow_ups")
    op.drop_table("follow_ups")

    _write_version(16, "0016")
