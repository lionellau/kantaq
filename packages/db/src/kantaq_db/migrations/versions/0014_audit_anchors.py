"""audit_anchors for E07-T5 (MOD-07 / FR-E07-5)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-16

The RFC 6962 Merkle anchor over a range of this replica's audit trail. A new
declared collection mirrored to both stores (D-07), append-only like
``audit_events`` and **off the sync allowlist** (each replica anchors its own
trail; architecture §2 has the backend hold anchors). Local infrastructure
(``schema_version`` etc.) is untouched — this is a collection table, so it gets
the standard envelope. Schema version 14; collection/allowlist counts go 16→17
declared, allowlist stays 12 (audit_anchors is NEVER_SYNC).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
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
        "audit_anchors",
        *_envelope(),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("range_start", sa.String(length=26), nullable=False),
        sa.Column("range_end", sa.String(length=26), nullable=False),
        sa.Column("merkle_root", sa.String(length=64), nullable=False),
        sa.Column("tree_size", sa.Integer(), nullable=False),
        sa.Column("chain_tip", sa.String(length=64), nullable=False),
        sa.Column("external_pin", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_anchors_actor_id"), "audit_anchors", ["actor_id"])
    op.create_index(op.f("ix_audit_anchors_range_start"), "audit_anchors", ["range_start"])
    op.create_index(op.f("ix_audit_anchors_range_end"), "audit_anchors", ["range_end"])

    _write_version(14, "0014")


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_anchors_range_end"), table_name="audit_anchors")
    op.drop_index(op.f("ix_audit_anchors_range_start"), table_name="audit_anchors")
    op.drop_index(op.f("ix_audit_anchors_actor_id"), table_name="audit_anchors")
    op.drop_table("audit_anchors")

    _write_version(13, "0013")
