"""Metrics API: the workspace-metrics / capacity surface (E20-T5, MOD-27).

``GET /v1/metrics/summary`` serves the locked ``WorkspaceMetrics`` contract for
Settings → Sync: counts, replica size by project, per-actor agent observability,
the non-dollar capacity gauge, and retention status. Curated like
``/v1/audit/range`` (E20-T3): per-actor rows respect the caller's scope —
``tokens.rotate`` sees every agent, everyone else sees only their own row.

The dashboard shows **capacity, not a dollar bill** (D-16): instead of a
projected cost it surfaces a ``billing_url`` deep-link into the Supabase console,
built from ``SUPABASE_URL`` (no new config). The backend footprint is the
estimate path (no service-role catalog read on a client — NFR-E24-1); the live
``measured`` path is reserved for a future service-role surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import metrics
from kantaq_core.identity import Action, VerifiedActor, can
from kantaq_runtime.auth import get_engine_dep, require_action
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]


class ProjectSizeOut(BaseModel):
    project_id: str
    name: str
    bytes: int
    rows: int


class ReplicaSizeOut(BaseModel):
    total_bytes: int
    by_project: list[ProjectSizeOut]


class CapacityOut(BaseModel):
    tier: str
    db_limit_bytes: int
    db_used_bytes: int
    db_pct: float
    egress_limit_bytes: int
    egress_used_bytes: int | None
    egress_pct: float | None
    headroom_warning: bool
    idle_pause_risk: bool


class BackendFootprintOut(BaseModel):
    measured: bool
    source: str
    rows: dict[str, Any]
    bytes: dict[str, Any]
    capacity: CapacityOut


class ActorUsageOut(BaseModel):
    actor_id: str
    role: str
    mcp_calls: int
    reads: int
    proposes: int
    denials: int
    est_payload_bytes: int
    est_tokens: int
    last_seen: datetime | None


class AgentActivityOut(BaseModel):
    window_days: int
    by_actor: list[ActorUsageOut]
    totals: ActorUsageOut


class RetentionStatusOut(BaseModel):
    audit_summarizable: int
    audit_anchored: bool
    sync_compactable_below_rev: int | None
    last_run: datetime | None
    next_run_due: datetime | None


class WorkspaceMetricsOut(BaseModel):
    generated_at: datetime
    hub_mode: str
    counts: dict[str, int]
    replica: ReplicaSizeOut
    backend: BackendFootprintOut | None
    agents: AgentActivityOut
    retention: RetentionStatusOut
    notes: list[str]
    # The "View billing in Supabase ↗" deep-link (D-16); None off Supabase.
    billing_url: str | None


def _usage_out(usage: metrics.ActorUsage) -> ActorUsageOut:
    return ActorUsageOut(
        actor_id=usage.actor_id,
        role=usage.role,
        mcp_calls=usage.mcp_calls,
        reads=usage.reads,
        proposes=usage.proposes,
        denials=usage.denials,
        est_payload_bytes=usage.est_payload_bytes,
        est_tokens=usage.est_tokens,
        last_seen=usage.last_seen,
    )


def _billing_url(settings: Settings) -> str | None:
    """The Supabase project's billing page, derived from SUPABASE_URL.

    ``https://<ref>.supabase.co`` → ``https://supabase.com/dashboard/project/<ref>/settings/billing``.
    Only on a Supabase backend: a local-only workspace has no project to bill.
    """
    from kantaq_runtime.config import HubMode

    url = settings.supabase_url
    if settings.hub_mode != HubMode.supabase or not url:
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0]
    ref = host.split(".", 1)[0]
    if not ref:
        return None
    return f"https://supabase.com/dashboard/project/{ref}/settings/billing"


@router.get("/summary", response_model=WorkspaceMetricsOut)
def metrics_summary(
    actor: ReaderActor, engine: EngineDep, request: Request, window_days: int = 30
) -> WorkspaceMetricsOut:
    settings: Settings = request.app.state.settings
    full = can(actor.role, Action.tokens_rotate, scopes=list(actor.scopes))
    with Session(engine) as session:
        m = metrics.summary(session, hub_mode=settings.hub_mode.value, window_days=window_days)

    # Scope gate (MOD-27): without tokens.rotate a caller sees only their own
    # per-actor row; the totals stay (an aggregate leaks no per-member detail).
    by_actor = (
        m.agents.by_actor
        if full
        else [u for u in m.agents.by_actor if u.actor_id == actor.member_id]
    )

    backend_out = None
    if m.backend is not None:
        cap = m.backend.capacity
        backend_out = BackendFootprintOut(
            measured=m.backend.measured,
            source=m.backend.source,
            rows=m.backend.rows,
            bytes=m.backend.bytes,
            capacity=CapacityOut(
                tier=cap.tier,
                db_limit_bytes=cap.db_limit_bytes,
                db_used_bytes=cap.db_used_bytes,
                db_pct=cap.db_pct,
                egress_limit_bytes=cap.egress_limit_bytes,
                egress_used_bytes=cap.egress_used_bytes,
                egress_pct=cap.egress_pct,
                headroom_warning=cap.headroom_warning,
                idle_pause_risk=cap.idle_pause_risk,
            ),
        )

    return WorkspaceMetricsOut(
        generated_at=m.generated_at,
        hub_mode=m.hub_mode,
        counts=m.counts,
        replica=ReplicaSizeOut(
            total_bytes=m.replica.total_bytes,
            by_project=[
                ProjectSizeOut(project_id=p.project_id, name=p.name, bytes=p.bytes, rows=p.rows)
                for p in m.replica.by_project
            ],
        ),
        backend=backend_out,
        agents=AgentActivityOut(
            window_days=m.agents.window_days,
            by_actor=[_usage_out(u) for u in by_actor],
            totals=_usage_out(m.agents.totals),
        ),
        retention=RetentionStatusOut(
            audit_summarizable=m.retention.audit_summarizable,
            audit_anchored=m.retention.audit_anchored,
            sync_compactable_below_rev=m.retention.sync_compactable_below_rev,
            last_run=m.retention.last_run,
            next_run_due=m.retention.next_run_due,
        ),
        notes=m.notes,
        billing_url=_billing_url(settings),
    )
