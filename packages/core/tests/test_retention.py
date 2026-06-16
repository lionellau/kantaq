"""MOD-27 / E26 retention: anchor-gated audit summarize + safe sync compaction."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core import audit, retention
from kantaq_db import LocalSetting

NOW = datetime(2026, 6, 1, 12, 0, 0)


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


def _seed_mcp_detail(session: Session, *, age_days: int, n: int = 3) -> None:
    ts = NOW - timedelta(days=age_days)
    for i in range(n):
        audit.write(
            session,
            actor_id="agent_bot",
            action="ticket.update",
            source="mcp",
            before={"x": 1},
            after={"x": 2},
            now=ts + timedelta(seconds=i),
        )
    session.commit()


def test_unanchored_range_is_refused(session: Session) -> None:
    _seed_mcp_detail(session, age_days=40, n=5)  # older than the 30-day TTL
    report = retention.run(session, now=NOW)
    session.commit()
    # v0.2 has no Merkle anchor → the audit half refuses, touching nothing.
    assert report.audit_summarized == 0
    assert report.audit_skipped_unanchored == 5
    # Originals are untouched (before/after intact).
    rows = audit.read_range(session, source="mcp")
    assert all(r.before is not None and r.after is not None for r in rows)


def test_fresh_detail_is_not_yet_expired(session: Session) -> None:
    _seed_mcp_detail(session, age_days=5, n=4)  # inside the window
    report = retention.run(session, now=NOW)
    assert report.audit_skipped_unanchored == 0  # nothing past the TTL yet


def test_sync_watermark_is_reported_not_deleted(session: Session) -> None:
    report = retention.run(session, now=NOW, safe_watermark_rev=42)
    assert report.sync_compactable_below_rev == 42  # reported; the DELETE is backend pg_cron


def test_sync_half_holds_without_a_watermark(session: Session) -> None:
    report = retention.run(session, now=NOW)  # no watermark surface → hold, never blind-delete
    assert report.sync_compactable_below_rev is None


def test_throttle_and_last_run_marker(session: Session) -> None:
    assert retention.last_run(session) is None
    assert retention.due(session, now=NOW) is True

    retention.run(session, now=NOW)
    session.commit()
    assert retention.last_run(session) == NOW
    # Stamped in local_settings, never synced.
    assert session.get(LocalSetting, retention.LAST_RUN_KEY) is not None

    # Within a day → not due; a day later → due again.
    assert retention.due(session, now=NOW + timedelta(hours=12)) is False
    assert retention.due(session, now=NOW + timedelta(days=1, minutes=1)) is True


def test_anchor_then_summarize_blanks_detail_and_keeps_the_chain(session: Session) -> None:
    """E07-T4b: with an actor to anchor, the audit half anchors-then-summarizes —
    expired mcp detail is blanked, verify_chain stays ok over the re-chained log,
    and a Merkle anchor was fixed over the original pre-retention range."""
    from kantaq_db import AuditAnchorRow

    _seed_mcp_detail(session, age_days=40, n=3)  # expired → summarized
    _seed_mcp_detail(session, age_days=5, n=1)  # fresh → untouched (scope control)

    report = retention.run(session, now=NOW, actor_id="mbr_owner")
    session.commit()

    assert report.audit_summarized == 3
    assert report.audit_skipped_unanchored == 0

    rows = audit.read_range(session, source="mcp")
    old = [r for r in rows if r.created_at < NOW - timedelta(days=30)]
    fresh = [r for r in rows if r.created_at >= NOW - timedelta(days=30)]
    assert len(old) == 3 and all(r.before is None and r.after is None for r in old)
    assert len(fresh) == 1 and all(r.before is not None for r in fresh)

    # The chain still verifies end-to-end — re-chained over the blanked content.
    assert audit.verify_chain(session).ok
    # An anchor was fixed over the original 3-row pre-retention range.
    anchor = session.exec(select(AuditAnchorRow)).first()
    assert anchor is not None and anchor.tree_size == 3


def test_summarize_is_idempotent_and_preserves_agent_read_summaries(session: Session) -> None:
    """A second run finds nothing left to blank; agent.read aggregates are never blanked."""
    _seed_mcp_detail(session, age_days=40, n=2)
    # An aggregated agent.read summary in the expired window — its `after` IS the
    # retained summary, so it must survive the prune.
    audit.write(
        session,
        actor_id="agent_bot",
        action="agent.read",
        source="mcp",
        after={"reads": 9, "objects": {"tickets/t1": 9}},
        now=NOW - timedelta(days=40),
    )
    session.commit()

    first = retention.run(session, now=NOW, actor_id="mbr_owner")
    session.commit()
    assert first.audit_summarized == 2

    second = retention.run(session, now=NOW, actor_id="mbr_owner")
    session.commit()
    assert second.audit_summarized == 0  # nothing left with before/after

    summaries = [r for r in audit.read_range(session, action="agent.read") if r.after]
    assert summaries and summaries[0].after == {"reads": 9, "objects": {"tickets/t1": 9}}
    assert audit.verify_chain(session).ok
