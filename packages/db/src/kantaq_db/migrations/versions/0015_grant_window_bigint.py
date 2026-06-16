"""widen capability_grants.issued_at/expires_at to BIGINT (DEBT-26)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-16

The grant-window columns were 32-bit INTEGER unix seconds, capping every grant
window at the Year-2038 ceiling. The v0.2 lifted human grant TTLs (E06-T7,
backend-issued) push toward that ceiling, so widen to 64-bit BIGINT — DEBT-26.
The signed *value* is unchanged (the RFC 8785 codec bounds ints at |n| ≤ 2^53−1),
so existing grant signatures stay valid; this only changes the column storage.

``batch_alter_table`` so SQLite recreates the column as BIGINT too (matching the
one D-07 model on both dialects); capability_grants has no incoming FK, so the
recreate is safe. On Postgres it is an in-place ALTER COLUMN TYPE.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
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
    with op.batch_alter_table("capability_grants") as batch:
        batch.alter_column(
            "issued_at", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False
        )
        batch.alter_column(
            "expires_at", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False
        )
    _write_version(15, "0015")


def downgrade() -> None:
    with op.batch_alter_table("capability_grants") as batch:
        batch.alter_column(
            "issued_at", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False
        )
        batch.alter_column(
            "expires_at", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False
        )
    _write_version(14, "0014")
