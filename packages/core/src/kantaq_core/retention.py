"""Retention: keep the audit + sync logs under the cost ceiling (MOD-27 / E26).

Two halves, both designed to **degrade safely** rather than lose data (MOD-27
§Retention, RISK-06):

1. **`audit_events` (`source="mcp"`) detail → summarized after 30 days.** The E07
   hash chain binds ``before``/``after`` into every link, so blanking detail and
   re-chaining would yield a chain that verifies but commits to the *stripped*
   content — with no proof the summary faithfully replaced the detail. So the
   prune **refuses any range that is not covered by a Merkle anchor** (E07-T5):
   ``run`` returns ``audit_skipped_unanchored`` and touches nothing. In v0.2 no
   anchor exists, so this half always refuses (the safe degrade); the
   anchor-and-summarize execution lands with E07-T5 via ``range_is_anchored``.

2. **`sync_events` → compacted after 30 days, below a safe watermark.** A
   wall-clock-only ``DELETE`` is wrong: replica cursors track by ``revision``, a
   replica offline > 30 days would converge from a gap (silent data loss), and
   ``sync_cursors`` are local per replica so a backend prune cannot see them. So
   ``run`` only *reports* ``sync_compactable_below_rev`` = the safe watermark
   (min acked revision across live replicas, surfaced by MOD-05); the actual
   ``DELETE`` runs backend-side (pg_cron / service-role), since ``sync_events``
   has no client delete grant. With no watermark the half **holds** (reports
   ``None``) — never deletes blind.

Scheduling is the caller's job (runtime startup + the 2 s sync-poll loop, MOD-27
§Retention 3); ``run`` stamps a ``retention.last_run`` marker and ``due`` gates
the once/day throttle so an idle runtime still prunes on its next tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_db import AuditEvent, LocalSetting

LAST_RUN_KEY = "retention.last_run"
THROTTLE = timedelta(days=1)


@dataclass(frozen=True)
class RetentionReport:
    audit_summarized: int  # mcp detail rows folded into summaries (0 until E07-T5 anchors a range)
    audit_skipped_unanchored: int  # expired mcp detail rows refused for lack of a Merkle anchor
    sync_compactable_below_rev: int | None  # the safe watermark to compact below; None = hold
    ran_at: datetime


def range_is_anchored(
    session: Session, *, now: datetime | None = None, audit_ttl_days: int = 30
) -> bool:
    """Whether the expired pre-retention audit range is covered by a Merkle anchor.

    Computes the cutoff and asks ``audit.range_is_anchored`` whether an anchor
    reaches the newest row older than it (E07-T5). ``True`` when nothing is
    expired yet — no range to prove. Read-only — the metrics dashboard's
    ``audit_anchored`` status calls it; ``run`` is what *creates* the anchor.
    """
    ts = _naive(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    cutoff = ts - timedelta(days=audit_ttl_days)
    end_id = session.exec(
        select(AuditEvent.id)
        .where(col(AuditEvent.created_at) < cutoff)
        .order_by(col(AuditEvent.id).desc())
        .limit(1)
    ).first()
    if end_id is None:
        return True
    return audit.range_is_anchored(session, end_id=end_id)


def last_run(session: Session) -> datetime | None:
    row = session.get(LocalSetting, LAST_RUN_KEY)
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def due(session: Session, *, now: datetime | None = None) -> bool:
    """True when retention has not run in the last day (the once/day throttle)."""
    ts = _naive(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    previous = last_run(session)
    return previous is None or (ts - previous) >= THROTTLE


def run(
    session: Session,
    *,
    now: datetime | None = None,
    audit_ttl_days: int = 30,
    sync_ttl_days: int = 30,
    safe_watermark_rev: int | None = None,
    actor_id: str | None = None,
) -> RetentionReport:
    """Anchor + summarize expired audit detail, and report the sync watermark.

    The audit half (E07-T4b): expired detailed MCP rows (``source="mcp"`` past
    the TTL, still carrying ``before``/``after``, excluding the ``agent.read``
    aggregates) are summarized — but **anchor first** (MOD-27): ``run`` fixes a
    Merkle anchor over the ORIGINAL pre-retention range, then blanks + re-chains
    via ``audit.summarize_rows``, so the prune stays provable. Without an
    ``actor_id`` to attribute the anchor the prune would be unprovable, so it
    **refuses** (``audit_skipped_unanchored``) — the documented safe degrade. The
    sync half only *reports* ``safe_watermark_rev`` (the DELETE is backend
    pg_cron). The transaction stays the caller's (flush, no commit).
    """
    ts = _naive(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    audit_cutoff = ts - timedelta(days=audit_ttl_days)

    detail = [
        row
        for row in session.exec(
            select(AuditEvent)
            .where(
                col(AuditEvent.source) == "mcp",
                col(AuditEvent.created_at) < audit_cutoff,
                col(AuditEvent.action) != audit.AGENT_READ_ACTION,
            )
            .order_by(col(AuditEvent.id).asc())
        ).all()
        if row.before is not None or row.after is not None
    ]

    summarized, skipped = 0, 0
    if detail:
        end_id = session.exec(
            select(AuditEvent.id)
            .where(col(AuditEvent.created_at) < audit_cutoff)
            .order_by(col(AuditEvent.id).desc())
            .limit(1)
        ).first()
        if actor_id is not None and end_id is not None:
            if not audit.range_is_anchored(session, end_id=end_id):
                audit.anchor_range(session, actor_id=actor_id, end_id=end_id, now=ts)
            summarized = audit.summarize_rows(session, row_ids=[r.id for r in detail], now=ts)
        else:
            skipped = len(detail)  # no actor to anchor with → cannot prove the prune

    _stamp_last_run(session, ts)
    return RetentionReport(
        audit_summarized=summarized,
        audit_skipped_unanchored=skipped,
        sync_compactable_below_rev=safe_watermark_rev,
        ran_at=ts,
    )


def _stamp_last_run(session: Session, ts: datetime) -> None:
    row = session.get(LocalSetting, LAST_RUN_KEY)
    if row is None:
        session.add(LocalSetting(key=LAST_RUN_KEY, value=ts.isoformat(), updated_at=ts))
    else:
        row.value = ts.isoformat()
        row.updated_at = ts
        session.add(row)
    session.flush()


def _naive(ts: datetime) -> datetime:
    if ts.tzinfo is not None:
        return ts.astimezone(UTC).replace(tzinfo=None)
    return ts
