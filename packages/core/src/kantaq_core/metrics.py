"""Workspace metrics & capacity gauge (MOD-27 / Epic E26, v0.2).

``summary`` is one read-only call that answers two questions a small team
actually has — *what did my agents do?* and *how full is my backend?* — locally,
offline, with no billing credentials (D-16: the dollar bill stays in the Supabase
console; this surfaces capacity, not cost). It returns the locked
``WorkspaceMetrics`` contract: row counts, the local SQLite replica size by
project, a per-actor agent-observability table, a **non-dollar** capacity gauge
against the Supabase Free 500 MB / 5 GB ceilings, and retention status.

The estimate uses **database catalog statistics** — SQLite ``dbstat`` for the
replica, Postgres ``pg_total_relation_size`` / ``pg_stat_user_tables`` for the
backend — so it needs no dependency, runs as read-only queries, and is
deterministic (the ≤10% accuracy gate, FR-E26-1, is testable against a seeded
dataset; see ``kantaq_test_harness.cost_profile`` + the calibration test). When
the backend cannot be measured (idle/paused/offline, or no engine handed in) the
footprint is **estimated from the local mirror** with a per-table byte model
calibrated at E26-T0 (−1.76% overall on the as-built 6-month profile).

Nothing here is persisted: ``WorkspaceMetrics`` is computed on demand and carries
counts and bytes, never ticket/memory content (the MOD-25 discipline). Timestamps
are injectable (``now=``) so tests drive them with FakeClock.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from kantaq_core.audit import AGENT_READ_ACTION
from kantaq_db import (
    AgentProposal,
    AuditEvent,
    Comment,
    EventLog,
    Member,
    MemoryEntry,
    MemoryLink,
    Project,
    Ticket,
    TicketRelationship,
    Workspace,
)

# --- Capacity constants (MOD-27 §Capacity) --------------------------------
# Decimal bytes (MB = 10**6, GB = 10**9), matching Supabase's published numbers,
# so the 80% threshold and the accuracy test are deterministic.
DB_LIMIT_FREE = 500_000_000
DB_LIMIT_PRO = 8_000_000_000
EGRESS_LIMIT_FREE = 5_000_000_000
EGRESS_LIMIT_PRO = 250_000_000_000
HEADROOM_THRESHOLD = 0.8  # db_pct >= 0.8 → "the free tier is about to bite"
IDLE_PAUSE_DAYS = 5  # tier=free + newest committed event older than this → at risk

# The closed 10-key `counts` set (MOD-27 contract): the 9 syncable domain
# collections + audit_events. MOD-12's renderer and this contract agree on
# exactly these keys. (tokens/devices/grants/skill_*/conflict_records are tables
# but not part of the operational counts the dashboard shows.)
_COUNT_MODELS: dict[str, type] = {
    "workspaces": Workspace,
    "projects": Project,
    "tickets": Ticket,
    "comments": Comment,
    "ticket_relationships": TicketRelationship,
    "members": Member,
    "agent_proposals": AgentProposal,
    "memory_entries": MemoryEntry,
    "memory_links": MemoryLink,
    "audit_events": AuditEvent,
}

# Per-table bytes/row model for the *estimate* path (measured=False). Each value
# is the total relation bytes/row (heap + indexes + TOAST) measured by
# pg_total_relation_size against the seeded as-built profile
# (kantaq_test_harness.seed_cost_profile), with incompressible content so the
# figure is a stable, conservative footprint. The FR-E26-1 gate asserts this
# model lands within 10% of the catalog on that profile; the catalog read
# (measured=True) is exact and ignores this. Real-bill calibration against live
# compressible data is a v0.3 follow-on (MOD-27 §Open questions, DEBT-03).
_BACKEND_ROW_BYTES: dict[str, int] = {
    "audit_events": 811,
    "sync_events": 753,
    "tickets": 2159,
    "comments": 1248,
    "memory_entries": 2345,
    "ticket_relationships": 401,
    "memory_links": 492,
    "agent_proposals": 986,
    "projects": 1802,
    "members": 540,
    "workspaces": 540,
}


# --- Contract dataclasses (MOD-27 §Interfaces, JSON-serialized for the API) ---
@dataclass(frozen=True)
class ProjectSize:
    project_id: str
    name: str
    bytes: int
    rows: int


@dataclass(frozen=True)
class ReplicaSize:
    """The local SQLite footprint (always present, all HUB_MODEs)."""

    total_bytes: int
    by_project: list[ProjectSize]


@dataclass(frozen=True)
class Capacity:
    """The non-dollar gauge: bytes/percent vs the tier ceiling, never a dollar."""

    tier: str  # free | pro | vps
    db_limit_bytes: int
    db_used_bytes: int
    db_pct: float
    egress_limit_bytes: int
    egress_used_bytes: int | None
    egress_pct: float | None
    headroom_warning: bool
    idle_pause_risk: bool


@dataclass(frozen=True)
class BackendFootprint:
    measured: bool  # True = read from the live catalog; False = modeled from the local mirror
    source: str  # catalog | metrics_api | estimate
    rows: dict[str, Any]  # {total, by_table}
    bytes: dict[str, Any]  # {total, by_table} — integer bytes
    capacity: Capacity


@dataclass(frozen=True)
class ActorUsage:
    actor_id: str
    role: str
    mcp_calls: int
    reads: int
    proposes: int
    denials: int
    est_payload_bytes: int
    est_tokens: int  # ceil(est_payload_bytes / 4) — a payload-size proxy, not model tokens
    last_seen: datetime | None


@dataclass(frozen=True)
class AgentActivity:
    window_days: int
    by_actor: list[ActorUsage]
    totals: ActorUsage


@dataclass(frozen=True)
class RetentionStatus:
    """What is prunable/summarized and when the job last/next runs (read view)."""

    audit_summarizable: int  # detailed mcp rows older than the TTL (the summarize target)
    audit_anchored: bool  # the pre-retention range has a Merkle anchor (E07-T5; False in v0.2)
    sync_compactable_below_rev: int | None  # the safe watermark, None when unknown
    last_run: datetime | None
    next_run_due: datetime | None


@dataclass(frozen=True)
class WorkspaceMetrics:
    generated_at: datetime
    hub_mode: str  # local | supabase | postgres
    counts: dict[str, int]
    replica: ReplicaSize
    backend: BackendFootprint | None  # None when hub_mode == local
    agents: AgentActivity
    retention: RetentionStatus
    notes: list[str]


# --- counts ---------------------------------------------------------------
def _counts(session: Session) -> dict[str, int]:
    """Row counts for the closed 10-key set, in local-replica order."""
    return {
        name: int(session.exec(select(func.count()).select_from(model)).one())
        for name, model in _COUNT_MODELS.items()
    }


# --- replica (SQLite dbstat) ---------------------------------------------
def _sqlite_table_bytes(session: Session) -> dict[str, int]:
    """Per-table bytes from SQLite ``dbstat`` (SUM(pgsize)); {} off SQLite."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return {}
    conn = session.connection()
    try:
        rows = conn.exec_driver_sql("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").all()
    except Exception:
        return {}  # dbstat not compiled in — fall back to PRAGMA total only
    return {name: int(size or 0) for name, size in rows}


def _sqlite_total_bytes(session: Session, table_bytes: dict[str, int]) -> int:
    """Whole-file bytes via ``PRAGMA page_count * page_size`` (the dbstat sum
    misses free pages); falls back to the table sum off SQLite."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return sum(table_bytes.values())
    conn = session.connection()
    page_count = int(conn.exec_driver_sql("PRAGMA page_count").scalar() or 0)
    page_size = int(conn.exec_driver_sql("PRAGMA page_size").scalar() or 0)
    return page_count * page_size


def _replica_size(session: Session, counts: dict[str, int]) -> ReplicaSize:
    """Local SQLite footprint + the by-project apportionment.

    Bytes are apportioned by row share (dbstat has no per-project tag): each
    project's share is its tickets + the comments on those tickets; everything
    not under a project (workspace/member/audit/memory/relationship/proposal
    rows) lands in the ``(unassigned)`` bucket. Shares sum to ``total_bytes``.
    """
    table_bytes = _sqlite_table_bytes(session)
    total_bytes = _sqlite_total_bytes(session, table_bytes)
    total_rows = sum(counts.values()) or 1

    projects = session.exec(select(Project)).all()
    by_project: list[ProjectSize] = []
    assigned_rows = 0
    assigned_bytes = 0
    for project in projects:
        tickets = int(
            session.exec(
                select(func.count()).select_from(Ticket).where(col(Ticket.project_id) == project.id)
            ).one()
        )
        comments = int(
            session.exec(
                select(func.count())
                .select_from(Comment)
                .join(Ticket, col(Comment.ticket_id) == col(Ticket.id))
                .where(col(Ticket.project_id) == project.id)
            ).one()
        )
        rows = tickets + comments
        proj_bytes = round(total_bytes * rows / total_rows)
        assigned_rows += rows
        assigned_bytes += proj_bytes
        by_project.append(
            ProjectSize(project_id=project.id, name=project.name, bytes=proj_bytes, rows=rows)
        )

    unassigned_rows = total_rows - assigned_rows if total_rows else 0
    # Remainder lands in (unassigned) so by_project bytes sum exactly to total.
    by_project.append(
        ProjectSize(
            project_id="(unassigned)",
            name="(unassigned)",
            bytes=total_bytes - assigned_bytes,
            rows=max(unassigned_rows, 0),
        )
    )
    return ReplicaSize(total_bytes=total_bytes, by_project=by_project)


# --- backend footprint ----------------------------------------------------
def _measure_backend(engine: Engine) -> tuple[dict[str, int], dict[str, int]]:
    """Exact rows + bytes from the live Postgres catalog (heap+index+TOAST)."""
    rows: dict[str, int] = {}
    sizes: dict[str, int] = {}
    with engine.connect() as conn:
        catalog = conn.execute(
            text(
                "SELECT relname, n_live_tup, "
                "pg_total_relation_size(relid) AS total_bytes "
                "FROM pg_stat_user_tables"
            )
        ).all()
    for relname, n_live, total in catalog:
        rows[str(relname)] = int(n_live or 0)
        sizes[str(relname)] = int(total or 0)
    return rows, sizes


def _estimate_backend(
    counts: dict[str, int], session: Session
) -> tuple[dict[str, int], dict[str, int]]:
    """Model rows + bytes from the local mirror (measured=False path).

    ``sync_events`` lives only on the backend; the local mirror's stand-in is the
    committed event-log count (the rows that became sync_events on commit).
    """
    rows = dict(counts)
    committed = int(
        session.exec(
            select(func.count())
            .select_from(EventLog)
            .where(col(EventLog.committed_rev).is_not(None))
        ).one()
    )
    rows["sync_events"] = committed
    return rows, model_backend_bytes(rows)


def model_backend_bytes(rows: dict[str, int]) -> dict[str, int]:
    """The per-table estimate model: rows × the E26-T0-calibrated bytes/row.

    Public for the FR-E26-1 accuracy test, which asserts it lands within 10% of
    ``pg_total_relation_size`` on the seeded profile (per-table and total).
    """
    return {table: count * _BACKEND_ROW_BYTES.get(table, 770) for table, count in rows.items()}


def _capacity(
    *,
    hub_mode: str,
    db_used: int,
    egress_used: int | None,
    idle_pause_risk: bool,
) -> Capacity:
    tier = _resolve_tier(hub_mode)
    if tier == "pro":
        db_limit, egress_limit = DB_LIMIT_PRO, EGRESS_LIMIT_PRO
    elif tier == "vps":
        db_limit = int(os.environ.get("KANTAQ_VPS_DISK_BYTES", "0") or 0)
        egress_limit = 0  # self-host egress is untracked
    else:  # free
        db_limit, egress_limit = DB_LIMIT_FREE, EGRESS_LIMIT_FREE

    db_pct = db_used / db_limit if db_limit else 0.0
    egress_pct: float | None = None
    if egress_used is not None and egress_limit:
        egress_pct = egress_used / egress_limit
    headroom = db_pct >= HEADROOM_THRESHOLD or (
        egress_pct is not None and egress_pct >= HEADROOM_THRESHOLD
    )
    return Capacity(
        tier=tier,
        db_limit_bytes=db_limit,
        db_used_bytes=db_used,
        db_pct=db_pct,
        egress_limit_bytes=egress_limit,
        egress_used_bytes=egress_used,
        egress_pct=egress_pct,
        headroom_warning=headroom,
        idle_pause_risk=idle_pause_risk and tier == "free",
    )


def _resolve_tier(hub_mode: str) -> str:
    if hub_mode == "postgres":
        return "vps"
    if hub_mode == "supabase":
        return os.environ.get("KANTAQ_SUPABASE_TIER", "free").lower()
    return "free"


def _newest_committed_at(session: Session) -> datetime | None:
    """Local-mirror timestamp of the newest committed event (idle-pause source)."""
    return session.exec(
        select(func.max(col(EventLog.created_at))).where(col(EventLog.committed_rev).is_not(None))
    ).one()


# --- agent observability --------------------------------------------------
def _agent_activity(
    session: Session, *, window_days: int, now: datetime
) -> tuple[AgentActivity, bool]:
    """Per-actor MCP observability from the audit log (no new capture).

    ``reads`` from ``agent.read`` summary rows' ``after.reads``; ``proposes`` from
    ``proposal.create``; ``denials`` from ``tool.deny``; ``mcp_calls`` is their
    sum. ``est_payload_bytes`` comes from the read summary's ``after.bytes`` once
    the MOD-08 gateway tally lands — until then it is 0 and a note is flagged.
    Returns the activity plus whether any payload bytes were seen.
    """
    cutoff = now - timedelta(days=window_days)
    mcp_rows = session.exec(
        select(AuditEvent).where(
            col(AuditEvent.source) == "mcp", col(AuditEvent.created_at) >= cutoff
        )
    ).all()

    @dataclass
    class _Acc:
        reads: int = 0
        proposes: int = 0
        denials: int = 0
        payload: int = 0
        last_seen: datetime | None = None

    accs: dict[str, _Acc] = {}
    payload_seen = False
    for row in mcp_rows:
        acc = accs.setdefault(row.actor_id, _Acc())
        if row.action == AGENT_READ_ACTION:
            after = row.after or {}
            acc.reads += int(after.get("reads", 0))
            if "bytes" in after:
                acc.payload += int(after.get("bytes", 0))
                payload_seen = True
        elif row.action == "proposal.create":
            acc.proposes += 1
        elif row.action == "tool.deny":
            acc.denials += 1
        if acc.last_seen is None or row.created_at > acc.last_seen:
            acc.last_seen = row.created_at

    roles = _member_roles(session)
    by_actor = [
        _actor_usage(actor_id, acc, roles.get(actor_id, "unknown"))
        for actor_id, acc in sorted(accs.items())
    ]
    totals = _totals(by_actor)
    return AgentActivity(window_days=window_days, by_actor=by_actor, totals=totals), payload_seen


def _actor_usage(actor_id: str, acc: Any, role: str) -> ActorUsage:
    calls = acc.reads + acc.proposes + acc.denials
    return ActorUsage(
        actor_id=actor_id,
        role=role,
        mcp_calls=calls,
        reads=acc.reads,
        proposes=acc.proposes,
        denials=acc.denials,
        est_payload_bytes=acc.payload,
        est_tokens=math.ceil(acc.payload / 4),
        last_seen=acc.last_seen,
    )


def _totals(by_actor: list[ActorUsage]) -> ActorUsage:
    last_seens = [a.last_seen for a in by_actor if a.last_seen is not None]
    payload = sum(a.est_payload_bytes for a in by_actor)
    return ActorUsage(
        actor_id="(all)",
        role="(all)",
        mcp_calls=sum(a.mcp_calls for a in by_actor),
        reads=sum(a.reads for a in by_actor),
        proposes=sum(a.proposes for a in by_actor),
        denials=sum(a.denials for a in by_actor),
        est_payload_bytes=payload,
        est_tokens=math.ceil(payload / 4),
        last_seen=max(last_seens) if last_seens else None,
    )


def _member_roles(session: Session) -> dict[str, str]:
    return {m.id: m.role for m in session.exec(select(Member)).all()}


# --- retention status (read view; the job lives in kantaq_core.retention) -
def _retention_status(
    session: Session, *, now: datetime, audit_ttl_days: int = 30
) -> RetentionStatus:
    from kantaq_core import retention  # local import: retention reads metrics' models

    cutoff = now - timedelta(days=audit_ttl_days)
    summarizable = int(
        session.exec(
            select(func.count())
            .select_from(AuditEvent)
            .where(col(AuditEvent.source) == "mcp", col(AuditEvent.created_at) < cutoff)
        ).one()
    )
    last_run = retention.last_run(session)
    next_due = last_run + timedelta(days=1) if last_run is not None else None
    return RetentionStatus(
        audit_summarizable=summarizable,
        audit_anchored=retention.range_is_anchored(session),
        sync_compactable_below_rev=None,  # the server-side watermark surface (MOD-05) is not wired
        last_run=last_run,
        next_run_due=next_due,
    )


# --- the public contract --------------------------------------------------
def summary(
    session: Session,
    *,
    hub_mode: str = "local",
    backend_engine: Engine | None = None,
    window_days: int = 30,
    now: datetime | None = None,
) -> WorkspaceMetrics:
    """Return the locked ``WorkspaceMetrics`` (MOD-27).

    ``hub_mode`` discriminates the backend: ``local`` → ``backend is None``;
    ``supabase``/``postgres`` → a footprint, **measured** from ``backend_engine``'s
    live catalog when one is handed in, else **estimated** from the local mirror.
    The tier (Free/Pro/VPS) and its limits resolve from ``hub_mode`` +
    ``KANTAQ_SUPABASE_TIER`` / ``KANTAQ_VPS_DISK_BYTES`` (MOD-27 §Capacity).
    """
    # The local replica stores naive-UTC; normalize so every comparison and the
    # contract's `generated_at` are naive-UTC (matches FakeClock + SQLite reads).
    ts = _as_naive(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    counts = _counts(session)
    replica = _replica_size(session, counts)
    agents, payload_seen = _agent_activity(session, window_days=window_days, now=ts)
    retention_status = _retention_status(session, now=ts)

    notes = ["est_tokens is a payload-size proxy, not the agent's model tokens"]
    if not payload_seen:
        notes.append("est_payload_bytes is 0 until the gateway payload tally lands (MOD-08)")

    backend: BackendFootprint | None = None
    if hub_mode != "local":
        if backend_engine is not None:
            rows, sizes = _measure_backend(backend_engine)
            measured, source = True, "catalog"
        else:
            rows, sizes = _estimate_backend(counts, session)
            measured, source = False, "estimate"
            notes.append("backend footprint is estimated from the local mirror (not measured live)")
        newest = _newest_committed_at(session)
        idle = newest is not None and (ts - _as_naive(newest)) > timedelta(days=IDLE_PAUSE_DAYS)
        capacity = _capacity(
            hub_mode=hub_mode,
            db_used=int(sum(sizes.values())),
            egress_used=None,  # egress is not locally measurable (catalog-blind) — see Supabase
            idle_pause_risk=idle,
        )
        notes.append("egress is not metered locally — see Supabase for the bill")
        backend = BackendFootprint(
            measured=measured,
            source=source,
            rows={"total": int(sum(rows.values())), "by_table": rows},
            bytes={"total": int(sum(sizes.values())), "by_table": sizes},
            capacity=capacity,
        )

    return WorkspaceMetrics(
        generated_at=ts,
        hub_mode=hub_mode,
        counts=counts,
        replica=replica,
        backend=backend,
        agents=agents,
        retention=retention_status,
        notes=notes,
    )


def _as_naive(ts: datetime) -> datetime:
    """SQLite reads strip tzinfo; compare like-with-like against a naive `now`."""
    if ts.tzinfo is not None:
        return ts.astimezone(UTC).replace(tzinfo=None)
    return ts
