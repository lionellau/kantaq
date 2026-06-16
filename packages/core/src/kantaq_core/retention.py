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

from sqlmodel import Session, col, func, select

from kantaq_db import AuditEvent, LocalSetting

LAST_RUN_KEY = "retention.last_run"
THROTTLE = timedelta(days=1)


@dataclass(frozen=True)
class RetentionReport:
    audit_summarized: int  # mcp detail rows folded into summaries (0 until E07-T5 anchors a range)
    audit_skipped_unanchored: int  # expired mcp detail rows refused for lack of a Merkle anchor
    sync_compactable_below_rev: int | None  # the safe watermark to compact below; None = hold
    ran_at: datetime


def range_is_anchored(session: Session) -> bool:
    """Whether the pre-retention audit range is covered by a Merkle anchor.

    The seam for E07-T5: when the v0.2 Merkle anchor (FR-E07-5) lands, this reads
    the anchor table and reports coverage over the to-be-summarized range. Until
    then no anchor mechanism exists, so the audit summarize half refuses (the
    documented safe degrade — MOD-27 §Dependencies).
    """
    return False


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
) -> RetentionReport:
    """Summarize expired audit detail (anchor-gated) + report sync compaction.

    The transaction stays the caller's (mirrors ``audit.write``); ``run`` flushes
    the ``retention.last_run`` marker but does not commit.
    """
    ts = _naive(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    audit_cutoff = ts - timedelta(days=audit_ttl_days)

    expired = int(
        session.exec(
            select(func.count())
            .select_from(AuditEvent)
            .where(col(AuditEvent.source) == "mcp", col(AuditEvent.created_at) < audit_cutoff)
        ).one()
    )

    if range_is_anchored(session):  # E07-T5 seam; False in v0.2 → the refuse path runs
        summarized, skipped = _summarize_anchored(session, audit_cutoff), 0
    else:
        summarized, skipped = 0, expired

    _stamp_last_run(session, ts)
    return RetentionReport(
        audit_summarized=summarized,
        audit_skipped_unanchored=skipped,
        sync_compactable_below_rev=safe_watermark_rev,
        ran_at=ts,
    )


def _summarize_anchored(session: Session, cutoff: datetime) -> int:  # pragma: no cover - E07-T5
    """Fold anchored expired MCP detail into summaries + re-anchor the chain.

    Reachable only once ``range_is_anchored`` returns True (E07-T5 Merkle anchor).
    Left unimplemented in v0.2 by design — the audit summarize half is gated on
    that anchor (MOD-27 §Dependencies); shipping a re-chaining below-app-layer
    mutation with no anchor to prove it against would be the exact unprovable
    prune the spec forbids.
    """
    raise NotImplementedError(
        "audit summarize requires the E07-T5 Merkle anchor; v0.2 refuses unanchored ranges"
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
