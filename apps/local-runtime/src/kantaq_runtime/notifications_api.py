"""Notifications config API (E20-T8 / MOD-12) — Settings → Notifications.

``GET /v1/notifications`` (``notifications.read``, every human) returns the
opt-in state, the sink type, and the sink **host** (never the URL's secret path —
a Slack incoming-webhook path carries a token). ``PUT /v1/notifications``
(``notifications.write``, Maintainer+ human) sets the sink + the opt-in.

Agents hold neither action (the scope ceiling excludes both, ``roles.py``), and
the PUT is human-only on top of that — so an agent can never enable or redirect
the dispatch. This is the config half of the "never widens permission" boundary.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import notifications
from kantaq_core.identity import Action, VerifiedActor
from kantaq_runtime.auth import get_engine_dep, require_action, require_human_action

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.notifications_read))]
# Setting the sink is a Maintainer+ human decision (and human-only on top of the
# role check — an agent can never configure the dispatch even with a wide token).
WriterActor = Annotated[VerifiedActor, Depends(require_human_action(Action.notifications_write))]


class NotificationOut(BaseModel):
    enabled: bool
    sink_type: str
    # The sink HOST only (hooks.slack.com), never the secret path — the response
    # carries no credential, mirroring the agent-snippet's no-token contract.
    sink_host: str | None
    configured: bool


class NotificationConfigIn(BaseModel):
    enabled: bool
    sink_type: str
    webhook_url: str | None = None


def _view(config: notifications.NotificationConfig) -> NotificationOut:
    # .hostname strips userinfo + port, so the response never carries a credential.
    host = urlsplit(config.webhook_url).hostname if config.webhook_url else None
    return NotificationOut(
        enabled=config.enabled,
        sink_type=config.sink_type,
        sink_host=host,
        configured=config.webhook_url is not None,
    )


@router.get("", response_model=NotificationOut)
def get_notifications(actor: ReaderActor, engine: EngineDep) -> NotificationOut:
    with Session(engine) as session:
        return _view(notifications.NotificationService(session).config())


@router.put("", response_model=NotificationOut)
def set_notifications(
    actor: WriterActor, engine: EngineDep, body: NotificationConfigIn
) -> NotificationOut:
    with Session(engine) as session:
        try:
            config = notifications.NotificationService(session).set_config(
                enabled=body.enabled,
                sink_type=body.sink_type,
                webhook_url=body.webhook_url,
                actor_id=actor.member_id,
            )
        except notifications.NotificationConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        session.commit()
        return _view(config)
