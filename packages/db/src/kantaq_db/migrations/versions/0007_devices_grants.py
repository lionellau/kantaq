"""devices + capability_grants for E06 v0.1 (MOD-06 / MOD-02)

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
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
        "devices",
        *_envelope(),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("member_id", sa.String(length=26), nullable=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_key"),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"]),
    )
    op.create_index(op.f("ix_devices_member_id"), "devices", ["member_id"], unique=False)

    op.create_table(
        "capability_grants",
        *_envelope(),
        sa.Column("subject", sa.String(length=26), nullable=False),
        sa.Column("issuer", sa.String(length=26), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("verbs", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("revokes", sa.String(), nullable=True),
        sa.Column("sig", sa.String(length=128), nullable=True),
        sa.Column("token_id", sa.String(length=26), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["subject"], ["members.id"]),
        sa.ForeignKeyConstraint(["issuer"], ["devices.id"]),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"]),
    )
    op.create_index(
        op.f("ix_capability_grants_subject"), "capability_grants", ["subject"], unique=False
    )
    op.create_index(
        op.f("ix_capability_grants_issuer"), "capability_grants", ["issuer"], unique=False
    )
    op.create_index(
        op.f("ix_capability_grants_token_id"), "capability_grants", ["token_id"], unique=False
    )

    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": 7, "revision": "0007", "applied_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_capability_grants_token_id"), table_name="capability_grants")
    op.drop_index(op.f("ix_capability_grants_issuer"), table_name="capability_grants")
    op.drop_index(op.f("ix_capability_grants_subject"), table_name="capability_grants")
    op.drop_table("capability_grants")
    op.drop_index(op.f("ix_devices_member_id"), table_name="devices")
    op.drop_table("devices")

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
