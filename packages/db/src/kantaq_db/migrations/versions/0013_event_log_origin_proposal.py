"""event_log.origin_proposal_id for E05-T3 (MOD-26 §B3 / FR-E05-3)

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-16

Adds the local-only ``origin_proposal_id`` column to ``event_log``: the
``agent_proposals`` row a ticket write was applied for, set when a human
approves an agent proposal. It lets ``flush_outbox`` tell a proposal-originated
ticket write apart from an ordinary human edit, so a stale-and-contending
proposal write is routed to ``rebase_required`` (bounced back to the human)
rather than minting a conflict_record — the §8.5 "agent never silently lands a
write whose base the team has moved past" rule.

Local infrastructure like ``sync_state`` (0011) / ``committed_rev``: ``event_log``
is the local SQLite log (Supabase holds ``sync_events``), so this column never
reaches Supabase and never enters a privacy envelope. No Supabase migration
ripples from it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
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
    # Nullable with no server_default: every existing row is a non-proposal
    # event (NULL), and a fresh ORM insert defaults to None.
    op.add_column(
        "event_log",
        sa.Column("origin_proposal_id", sa.String(length=26), nullable=True),
    )
    _write_version(13, "0013")


def downgrade() -> None:
    op.drop_column("event_log", "origin_proposal_id")
    _write_version(12, "0012")
