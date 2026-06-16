"""MOD-27 / E26-T1: workspace metrics, agent observability, capacity gauge."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core import audit, metrics
from kantaq_db import (
    Comment,
    EventLog,
    Member,
    Project,
    Ticket,
    Workspace,
)

NOW = datetime(2026, 6, 1, 12, 0, 0)


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


def _seed_small(session: Session) -> None:
    session.add(Workspace(id="ws1", name="Acme"))
    session.add(Member(id="mbr_human", workspace_id="ws1", email="a@x.com", role="Maintainer"))
    session.add(Member(id="agent_bot", workspace_id="ws1", email="bot@x.com", role="Agent"))
    session.add(Project(id="prj1", workspace_id="ws1", name="Alpha"))
    session.add(Project(id="prj2", workspace_id="ws1", name="Beta"))
    session.add(Ticket(id="tkt1", project_id="prj1", title="t1"))
    session.add(Ticket(id="tkt2", project_id="prj1", title="t2"))
    session.add(Ticket(id="tkt3", project_id="prj2", title="t3"))
    session.add(Comment(id="cmt1", ticket_id="tkt1", author_actor_id="mbr_human", body="hi"))
    session.commit()


def test_counts_are_the_closed_ten_key_set(session: Session) -> None:
    _seed_small(session)
    m = metrics.summary(session, now=NOW)
    assert set(m.counts) == {
        "workspaces",
        "projects",
        "tickets",
        "comments",
        "ticket_relationships",
        "members",
        "agent_proposals",
        "memory_entries",
        "memory_links",
        "audit_events",
    }
    assert m.counts["tickets"] == 3
    assert m.counts["projects"] == 2
    assert m.counts["members"] == 2
    assert m.counts["comments"] == 1


def test_local_mode_has_no_backend(session: Session) -> None:
    _seed_small(session)
    m = metrics.summary(session, hub_mode="local", now=NOW)
    assert m.hub_mode == "local"
    assert m.backend is None  # solo → no shared cost
    assert m.generated_at == NOW


def test_replica_by_project_sums_to_total(session: Session) -> None:
    _seed_small(session)
    m = metrics.summary(session, now=NOW)
    assert m.replica.total_bytes > 0
    assert sum(p.bytes for p in m.replica.by_project) == m.replica.total_bytes
    names = {p.project_id for p in m.replica.by_project}
    assert "(unassigned)" in names
    assert {"prj1", "prj2"} <= names


def test_supabase_estimate_capacity_gauge_free_tier(session: Session, monkeypatch) -> None:
    _seed_small(session)
    monkeypatch.delenv("KANTAQ_SUPABASE_TIER", raising=False)
    m = metrics.summary(session, hub_mode="supabase", now=NOW)
    assert m.backend is not None
    assert m.backend.measured is False  # no engine handed in → estimated from the mirror
    assert m.backend.source == "estimate"
    cap = m.backend.capacity
    assert cap.tier == "free"
    assert cap.db_limit_bytes == metrics.DB_LIMIT_FREE
    assert cap.egress_limit_bytes == metrics.EGRESS_LIMIT_FREE
    assert cap.db_used_bytes == m.backend.bytes["total"]
    assert any("estimated from the local mirror" in n for n in m.notes)


def test_pro_and_vps_tiers(session: Session, monkeypatch) -> None:
    _seed_small(session)
    monkeypatch.setenv("KANTAQ_SUPABASE_TIER", "pro")
    pro = metrics.summary(session, hub_mode="supabase", now=NOW)
    assert pro.backend is not None and pro.backend.capacity.tier == "pro"
    assert pro.backend.capacity.db_limit_bytes == metrics.DB_LIMIT_PRO

    monkeypatch.setenv("KANTAQ_VPS_DISK_BYTES", "50000000000")
    vps = metrics.summary(session, hub_mode="postgres", now=NOW)
    assert vps.backend is not None and vps.backend.capacity.tier == "vps"
    assert vps.backend.capacity.db_limit_bytes == 50_000_000_000
    assert vps.backend.capacity.egress_pct is None  # vps egress untracked


def test_headroom_warning_fires_over_80pct(session: Session, monkeypatch) -> None:
    _seed_small(session)
    monkeypatch.setenv("KANTAQ_VPS_DISK_BYTES", "1000")  # tiny ceiling → estimate blows past 80%
    m = metrics.summary(session, hub_mode="postgres", now=NOW)
    assert m.backend is not None
    assert m.backend.capacity.db_pct > 0.8
    assert m.backend.capacity.headroom_warning is True


def test_agent_observability_from_audit(session: Session) -> None:
    _seed_small(session)
    # An agent.read summary carries reads + (once the gateway hook lands) bytes.
    audit.write(
        session,
        actor_id="agent_bot",
        action=audit.AGENT_READ_ACTION,
        source="mcp",
        after={"reads": 12, "objects": {}, "bytes": 4096},
        now=NOW,
    )
    audit.write(session, actor_id="agent_bot", action="proposal.create", source="mcp", now=NOW)
    audit.write(session, actor_id="agent_bot", action="tool.deny", source="mcp", now=NOW)
    session.commit()

    m = metrics.summary(session, now=NOW)
    by_id = {a.actor_id: a for a in m.agents.by_actor}
    bot = by_id["agent_bot"]
    assert bot.role == "Agent"
    assert bot.reads == 12
    assert bot.proposes == 1
    assert bot.denials == 1
    assert bot.mcp_calls == 14  # reads + proposes + denials
    assert bot.est_payload_bytes == 4096
    assert bot.est_tokens == 1024  # ceil(4096 / 4)
    assert m.agents.totals.mcp_calls == 14
    # bytes were seen → the "0 until the gateway tally lands" note is absent
    assert not any("est_payload_bytes is 0" in n for n in m.notes)
    assert any("payload-size proxy" in n for n in m.notes)


def test_est_payload_bytes_zero_flagged_when_no_gateway_tally(session: Session) -> None:
    _seed_small(session)
    audit.write(
        session,
        actor_id="agent_bot",
        action=audit.AGENT_READ_ACTION,
        source="mcp",
        after={"reads": 3, "objects": {}},
        now=NOW,
    )  # no "bytes"
    session.commit()
    m = metrics.summary(session, now=NOW)
    bot = {a.actor_id: a for a in m.agents.by_actor}["agent_bot"]
    assert bot.est_payload_bytes == 0
    assert any("est_payload_bytes is 0" in n for n in m.notes)


def test_observability_window_excludes_old_rows(session: Session) -> None:
    _seed_small(session)
    audit.write(
        session,
        actor_id="agent_bot",
        action="proposal.create",
        source="mcp",
        now=NOW - timedelta(days=40),
    )  # outside the 30-day window
    audit.write(
        session,
        actor_id="agent_bot",
        action="proposal.create",
        source="mcp",
        now=NOW - timedelta(days=5),
    )  # inside
    session.commit()
    m = metrics.summary(session, window_days=30, now=NOW)
    bot = {a.actor_id: a for a in m.agents.by_actor}["agent_bot"]
    assert bot.proposes == 1  # only the in-window row


def test_idle_pause_risk_on_stale_free_backend(session: Session, monkeypatch) -> None:
    _seed_small(session)
    monkeypatch.delenv("KANTAQ_SUPABASE_TIER", raising=False)
    # A committed event 9 days old → past the 5-day idle-pause warning on Free.
    session.add(
        EventLog(
            event_id="e1",
            collection="tickets",
            entity_id="tkt1",
            actor_id="mbr_human",
            actor_seq=1,
            op="patch",
            committed_rev=1,
            created_at=NOW - timedelta(days=9),
        )
    )
    session.commit()
    m = metrics.summary(session, hub_mode="supabase", now=NOW)
    assert m.backend is not None
    assert m.backend.capacity.idle_pause_risk is True
