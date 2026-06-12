"""Telemetry API: the opt-in toggle and the local inspection view (E28, MOD-25).

``GET /v1/telemetry`` is the inspection surface (FR-E28-3) — the toggle state,
the computed outcome metrics, and the raw captured events, so a user can see
*exactly* what would ever be shared (D-10: nothing leaves the machine; sharing
is a manual act). Every human role may read it: the privacy promise is
transparency, so inspection is never admin-gated. ``PUT /v1/telemetry`` flips
the opt-in (Maintainer+, ``telemetry.write``) and audits the flip.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.telemetry import TelemetryService
from kantaq_db.models import TelemetryEvent
from kantaq_runtime.auth import get_engine_dep, require_action

router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.telemetry_read))]
WriterActor = Annotated[VerifiedActor, Depends(require_action(Action.telemetry_write))]


class TelemetryEventOut(BaseModel):
    id: str
    name: str
    props: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_row(cls, row: TelemetryEvent) -> TelemetryEventOut:
        return cls.model_validate(row, from_attributes=True)


class TelemetryMetricsOut(BaseModel):
    events_total: int
    proposal_acceptance_rate: float | None
    median_seconds_to_approve: float | None
    mcp_sessions_total: int
    repeat_session_members: int
    activity_views_total: int
    install_to_first_proposal_seconds: float | None
    weekly_active: bool


class TelemetryOut(BaseModel):
    enabled: bool
    metrics: TelemetryMetricsOut
    events: list[TelemetryEventOut]


class TelemetryToggleIn(BaseModel):
    enabled: bool


def _view(session: Session) -> TelemetryOut:
    service = TelemetryService(session)
    metrics = service.metrics()
    return TelemetryOut(
        enabled=service.enabled(),
        metrics=TelemetryMetricsOut(
            events_total=metrics.events_total,
            proposal_acceptance_rate=metrics.proposal_acceptance_rate,
            median_seconds_to_approve=metrics.median_seconds_to_approve,
            mcp_sessions_total=metrics.mcp_sessions_total,
            repeat_session_members=metrics.repeat_session_members,
            activity_views_total=metrics.activity_views_total,
            install_to_first_proposal_seconds=metrics.install_to_first_proposal_seconds,
            weekly_active=metrics.weekly_active,
        ),
        events=[TelemetryEventOut.from_row(row) for row in service.events()],
    )


@router.get("", response_model=TelemetryOut)
def inspect_telemetry(actor: ReaderActor, engine: EngineDep) -> TelemetryOut:
    with Session(engine) as session:
        return _view(session)


@router.put("", response_model=TelemetryOut)
def toggle_telemetry(
    body: TelemetryToggleIn, actor: WriterActor, engine: EngineDep
) -> TelemetryOut:
    with Session(engine) as session:
        TelemetryService(session).set_enabled(body.enabled, actor_id=actor.member_id)
        session.commit()
        return _view(session)
