"""conflict_records for E05-T2 (MOD-26 §B4)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-15

The resolvable same-scalar conflict collection. A new syncable, backend-
authoritative (authoritative_tx) collection: minted at the merge, never written
optimistically by a client. The ``id`` is deterministic (a domain-separated hash
of entity_id + field + the contending revisions, computed in Python), so this
table just stores what the mint produced. ``event_log.sync_state`` already
shipped in 0011, so this migration is conflict_records only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
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
        "conflict_records",
        *_envelope(),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("collection", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("contending_revisions", sa.JSON(), nullable=False),
        sa.Column("candidate_values", sa.JSON(), nullable=False),
        sa.Column("base_rev", sa.Integer(), nullable=False),
        sa.Column("head_rev", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=26), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_by", sa.String(length=26), nullable=True),
        sa.Column("resolved_choice", sa.String(length=16), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_conflict_records_workspace_id"), "conflict_records", ["workspace_id"])
    op.create_index(op.f("ix_conflict_records_entity_id"), "conflict_records", ["entity_id"])

    _write_version(12, "0012")


def downgrade() -> None:
    op.drop_index(op.f("ix_conflict_records_entity_id"), table_name="conflict_records")
    op.drop_index(op.f("ix_conflict_records_workspace_id"), table_name="conflict_records")
    op.drop_table("conflict_records")

    _write_version(11, "0011")
