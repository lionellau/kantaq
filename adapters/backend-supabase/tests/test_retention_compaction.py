"""E07-T4a: watermark-safe sync_events compaction (FR-E26-2, MOD-05 + MOD-27).

The compaction (``kantaq.compact_sync_events``) deletes only rows BOTH below the
safe ack watermark AND older than the TTL, via the one sanctioned below-app-layer
path (the GUC the append-only trigger checks). These run on real Postgres
(EphemeralPostgres) and prove the safety the spec demands:

- a lagging-but-live replica is never stranded (nothing at/above its ack is cut);
- the negative the watermark fixes: a wall-clock-only prune WOULD delete a row a
  lagging replica still needs;
- a replica silent past the TTL is excluded (it re-snapshots, not hold-back);
- the trigger still blocks every other mutation — a non-GUC DELETE, and UPDATE /
  TRUNCATE even WITH the GUC — so v0.2's strict immutability holds everywhere else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

WS = "ws_ret0000000000000000000"
OLD = datetime(2026, 1, 1, tzinfo=UTC)  # well past any 30-day cutoff at test time
FRESH = datetime.now(UTC)


def _seed_workspace(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "insert into workspaces (id, created_at, updated_at, actor_seq, visibility,"
                " hosting_mode, retention_policy, name) values"
                " (:id, now(), now(), 0, 'team', 'plain', 'standard', 'Retention')"
            ),
            {"id": WS},
        )


def _add_event(engine: Engine, *, seq: int, committed_at: datetime) -> int:
    """Insert one sync_events row into WS at an explicit committed_at; return its revision."""
    with engine.begin() as c:
        rev = c.execute(
            text(
                "insert into sync_events (event_id, collection, entity_id, actor_id, actor_seq,"
                " op, payload, workspace_id, committed_at) values"
                " (:eid, 'tickets', :ent, 'mbr_ret', :seq, 'patch', '{}'::json, :ws, :ts)"
                " returning revision"
            ),
            {
                "eid": f"evt_ret_{seq:018d}",
                "ent": f"tkt_{seq}",
                "seq": seq,
                "ws": WS,
                "ts": committed_at,
            },
        ).scalar_one()
    return int(rev)


def _ack(engine: Engine, replica: str, acked_rev: int, *, updated_at: datetime) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "insert into sync_acks (workspace_id, member_id, replica_id, acked_rev, updated_at)"
                " values (:ws, 'mbr_ret', :r, :rev, :ts)"
                " on conflict (workspace_id, member_id, replica_id) do update set"
                " acked_rev = excluded.acked_rev, updated_at = excluded.updated_at"
            ),
            {"ws": WS, "r": replica, "rev": acked_rev, "ts": updated_at},
        )


def _revisions(engine: Engine) -> set[int]:
    with engine.begin() as c:
        rows = c.execute(
            text("select revision from sync_events where workspace_id = :ws"), {"ws": WS}
        ).scalars()
        return set(rows)


def _compact(engine: Engine, ttl_days: int = 30) -> int:
    with engine.begin() as c:
        result = c.execute(text("select kantaq.compact_sync_events(:t)"), {"t": ttl_days})
        return int(result.scalar_one())


def test_lagging_live_replica_is_never_stranded(sync_pg: Engine) -> None:
    _seed_workspace(sync_pg)
    r1 = _add_event(sync_pg, seq=1, committed_at=OLD)  # old, below watermark
    r2 = _add_event(sync_pg, seq=2, committed_at=OLD)  # old, AT the watermark
    r3 = _add_event(sync_pg, seq=3, committed_at=OLD)  # old, ABOVE the watermark
    # Replica A pulled up to r3; replica B lags at r2. Both live (acked now).
    _ack(sync_pg, "replica_a", r3, updated_at=FRESH)
    _ack(sync_pg, "replica_b", r2, updated_at=FRESH)

    deleted = _compact(sync_pg)

    remaining = _revisions(sync_pg)
    assert r1 not in remaining, "a row every replica already pulled should be compacted"
    assert r2 in remaining, "the boundary row (== watermark) is kept — conservative"
    assert r3 in remaining, "the lagging replica B still needs r3 — it must survive"
    assert deleted >= 1


def test_a_wall_clock_only_prune_would_lose_data(sync_pg: Engine) -> None:
    """The negative the watermark fixes: r3 is OLD but a live replica hasn't pulled it."""
    _seed_workspace(sync_pg)
    _add_event(sync_pg, seq=1, committed_at=OLD)
    r2 = _add_event(sync_pg, seq=2, committed_at=OLD)
    r3 = _add_event(sync_pg, seq=3, committed_at=OLD)  # old AND above the watermark
    _ack(sync_pg, "replica_a", r3, updated_at=FRESH)
    _ack(sync_pg, "replica_b", r2, updated_at=FRESH)  # watermark = r2

    cutoff = datetime.now(UTC) - timedelta(days=30)
    with sync_pg.begin() as c:
        wall_clock_doomed = c.execute(
            text(
                "select count(*) from sync_events where workspace_id = :ws"
                " and committed_at < :cut and revision > "
                "(select min(acked_rev) from sync_acks where workspace_id = :ws)"
            ),
            {"ws": WS, "cut": cutoff},
        ).scalar_one()
    # A naive committed_at-only prune would delete r3 — a row replica B still needs.
    assert wall_clock_doomed >= 1

    _compact(sync_pg)
    assert r3 in _revisions(sync_pg), "the watermark-safe prune must keep r3 for replica B"


def test_a_replica_silent_past_the_ttl_is_excluded(sync_pg: Engine) -> None:
    """A too-stale replica re-snapshots; it does not hold the prune back forever."""
    _seed_workspace(sync_pg)
    r1 = _add_event(sync_pg, seq=1, committed_at=OLD)
    r2 = _add_event(sync_pg, seq=2, committed_at=OLD)
    # The only LIVE replica is at r2; a stale replica acked r1 long ago.
    _ack(sync_pg, "replica_live", r2, updated_at=FRESH)
    _ack(sync_pg, "replica_stale", r1, updated_at=datetime(2025, 1, 1, tzinfo=UTC))

    _compact(sync_pg)

    remaining = _revisions(sync_pg)
    assert r1 not in remaining, "the stale replica's low ack must not pin r1 forever"
    assert r2 in remaining


def test_nothing_is_compacted_without_a_live_ack(sync_pg: Engine) -> None:
    _seed_workspace(sync_pg)
    r1 = _add_event(sync_pg, seq=1, committed_at=OLD)
    # No sync_acks row at all → watermark is NULL → the workspace is skipped.
    deleted = _compact(sync_pg)
    assert r1 in _revisions(sync_pg)
    assert deleted == 0


def test_fresh_rows_below_the_watermark_survive(sync_pg: Engine) -> None:
    _seed_workspace(sync_pg)
    r1 = _add_event(sync_pg, seq=1, committed_at=FRESH)  # below watermark but NEW
    r2 = _add_event(sync_pg, seq=2, committed_at=OLD)
    _ack(sync_pg, "replica_a", r2 + 10, updated_at=FRESH)  # watermark above both

    _compact(sync_pg)

    remaining = _revisions(sync_pg)
    assert r1 in remaining, "a row newer than the TTL is never compacted, even below the watermark"
    assert r2 not in remaining


def test_the_log_stays_immutable_outside_the_sanctioned_delete(sync_pg: Engine) -> None:
    """The trigger bypass is DELETE-only: a non-GUC delete, and UPDATE/TRUNCATE
    even with the GUC, all still raise — strict immutability holds elsewhere."""
    _seed_workspace(sync_pg)
    rev = _add_event(sync_pg, seq=1, committed_at=OLD)

    # A direct DELETE without the retention GUC is refused.
    with pytest.raises(Exception, match="append-only"), sync_pg.begin() as c:  # noqa: PT011
        c.execute(text("delete from sync_events where revision = :r"), {"r": rev})

    # Even WITH the GUC set, UPDATE and TRUNCATE are still refused (DELETE-only bypass).
    with pytest.raises(Exception, match="append-only"), sync_pg.begin() as c:  # noqa: PT011
        c.execute(text("set local kantaq.retention_compaction = 'on'"))
        c.execute(text("update sync_events set op = 'tombstone' where revision = :r"), {"r": rev})
    with pytest.raises(Exception, match="append-only"), sync_pg.begin() as c:  # noqa: PT011
        c.execute(text("set local kantaq.retention_compaction = 'on'"))
        c.execute(text("truncate sync_events"))

    assert rev in _revisions(sync_pg), "the row survived every blocked mutation"
