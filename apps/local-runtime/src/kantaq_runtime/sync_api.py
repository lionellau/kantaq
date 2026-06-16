"""Sync status API: where committed state stands for this replica (E20-T2, MOD-04/MOD-12).

A read-only status surface for Settings → Sync. **It moves no data.** The
push/pull engine is deliberately unwired until Sprint 4 (the device-event seam
is in place, but emitting now would poison the push queue), so v0.1 has no
"sync now" action and this endpoint invents none — surfacing a button that
did nothing would be dishonest. What it reports is honest and local: the
configured backend mode (``HUB_MODE``, MOD-14), whether a remote backend is
configured, and the state of the local event log — how many events are still
local-only (``committed_rev IS NULL``) versus acknowledged by a backend, and
when the last commit landed (``None`` until Sprint 4 wires sync).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from kantaq_core.identity import VerifiedActor
from kantaq_db.models import EventLog
from kantaq_runtime.auth import get_engine_dep, require_actor
from kantaq_runtime.config import HubMode, Settings

router = APIRouter(prefix="/v1/sync", tags=["sync"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]


class SyncStatusOut(BaseModel):
    hub_mode: str
    backend_configured: bool
    pending_events: int
    committed_events: int
    total_events: int
    last_committed_at: datetime | None
    # The active agent-proposal staleness policy (MOD-26 §B3 / E05-T3), surfaced
    # read-only so the team sees how a stale approved proposal is handled.
    agent_proposal_stale_policy: str


def _backend_configured(settings: Settings) -> bool:
    """Whether a remote sync target is configured for the current mode.

    ``local`` keeps everything on this machine — no remote backend by design.
    Self-hosted ``postgres`` lands in v0.3; until then only ``supabase`` carries
    a URL.
    """
    if settings.hub_mode == HubMode.supabase:
        return bool(settings.supabase_url)
    return False


@router.get("/status", response_model=SyncStatusOut)
def sync_status(actor: AnyActor, engine: EngineDep, request: Request) -> SyncStatusOut:
    settings: Settings = request.app.state.settings
    with Session(engine) as session:
        # session.scalar (not exec(...).one()) so func.count()/func.max() come
        # back as a bare int / datetime rather than a one-tuple Row.
        total = session.scalar(select(func.count()).select_from(EventLog)) or 0
        pending = (
            session.scalar(
                select(func.count())
                .select_from(EventLog)
                .where(col(EventLog.committed_rev).is_(None))
            )
            or 0
        )
        last_committed = session.scalar(
            select(func.max(EventLog.created_at)).where(col(EventLog.committed_rev).is_not(None))
        )
    return SyncStatusOut(
        hub_mode=settings.hub_mode.value,
        backend_configured=_backend_configured(settings),
        pending_events=pending,
        committed_events=total - pending,
        total_events=total,
        last_committed_at=last_committed,
        agent_proposal_stale_policy=settings.agent_proposal_stale_policy.value,
    )
