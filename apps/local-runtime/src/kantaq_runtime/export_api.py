"""Export API: the portable workspace bundle (E23, MOD-23, FR-E23-1..3).

``POST /v1/export[?since=<rev>]`` returns the deterministic gzip tarball the
producer (``kantaq_runtime.export``) builds. An export reads the whole
workspace, so it needs ``tickets.read`` and rides the runtime's loopback auth
(origin + bearer) like every ``/v1/*`` route. The device key (when the runtime
has one) signs the bundle manifest; ``?since`` makes the event streams
incremental. ``/v1/import`` is v0.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import Action, VerifiedActor, device_private_key
from kantaq_runtime.auth import get_engine_dep, keychain_for, require_action
from kantaq_runtime.config import Settings
from kantaq_runtime.export import ExportError, build_bundle
from kantaq_runtime.tracker_api import blob_store_for

router = APIRouter(prefix="/v1", tags=["export"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]


@router.post("/export")
def export_bundle(
    request: Request,
    actor: ReaderActor,
    engine: EngineDep,
    since: Annotated[int | None, Query(ge=0)] = None,
) -> Response:
    """Produce the portable bundle; ``since`` exports only the committed delta."""
    settings: Settings = request.app.state.settings
    device_key = device_private_key(keychain_for(settings))
    with Session(engine) as session:
        try:
            bundle = build_bundle(
                session,
                blob_store=blob_store_for(settings),
                now=datetime.now(UTC),
                device_key=device_key,
                since=since,
            )
        except ExportError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=bundle,
        media_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="kantaq-export.tar.gz"'},
    )
