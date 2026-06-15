"""event_log.sync_state for E05-T1 (MOD-26 §B1 / FR-E05-1)

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-15

Adds the durable-outbox terminal-state column to ``event_log``: an offline
write appends a ``pending`` event; the backend ack flips it to ``committed``, a
CAS-rejected ``authoritative_tx`` / verify-failed event to ``rejected``, and a
stale agent proposal to ``rebase_required``. The outbox query becomes
``committed_rev IS NULL AND sync_state = 'pending'`` so a never-acceptable event
leaves the outbox instead of being re-pushed forever (the zombie-event hole).

Local infrastructure like ``committed_rev``: ``event_log`` is the local SQLite
log (the Supabase backend table is ``sync_events``), so this column never
reaches Supabase and never enters a privacy envelope. No Supabase migration
ripples from it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
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
    # server_default backfills pre-E05 rows as 'pending'; the model default is
    # also "pending" so a fresh ORM insert needs no explicit value.
    op.add_column(
        "event_log",
        sa.Column("sync_state", sa.String(length=16), nullable=False, server_default="pending"),
    )
    _write_version(11, "0011")


def downgrade() -> None:
    op.drop_column("event_log", "sync_state")
    _write_version(10, "0010")
