"""Runtime notification dispatch — the HTTP half of E20-T8 (MOD-12 / PRD §16.10).

A content-free outbound POST to the workspace-configured sink, with bounded
retries and a dead-letter. Fired **post-response** via FastAPI ``BackgroundTasks``
(see ``proposals_api``), so a slow or dead sink never blocks an approve/reject.

SEC boundary (second-model review surface): the body is built ONLY by
``kantaq_core.notifications.content_free_payload`` — ids + action + actor +
deep-link — so no ticket or memory content can ever reach this module. The
dispatch never raises into its caller, never widens permission, and carries no
token; the audit row records the sink HOST + outcome, never the URL's secret
path and never a body.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.notifications import (
    NotificationConfig,
    NotificationEvent,
    NotificationService,
    content_free_payload,
)
from kantaq_db.models import NotificationDeadLetter

logger = logging.getLogger("kantaq.notifications")

_MAX_ATTEMPTS = 4
_TIMEOUT_S = 5.0
_BACKOFF_BASE_S = 0.5
_BACKOFF_CAP_S = 4.0


def _slack_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-shape the content-free payload as a Slack message.

    Still content-free: the text is action + ids + actor + the deep-link only —
    every token comes from ``content_free_payload``, none from a ticket body.
    """
    ids = ", ".join(payload["ids"])
    return {
        "text": (
            f"kantaq: {payload['action']} ({ids}) by {payload['actor']} — {payload['deep_link']}"
        )
    }


def _sink_body(config: NotificationConfig, payload: dict[str, Any]) -> dict[str, Any]:
    return _slack_body(payload) if config.sink_type == "slack" else payload


def _audit_dispatch(
    session: Session,
    event: NotificationEvent,
    config: NotificationConfig,
    *,
    status: str,
    attempts: int,
    now: Callable[[], datetime],
    error: str = "",
) -> None:
    """Record the dispatch outcome — metadata only, never a body or the secret URL."""
    after: dict[str, Any] = {
        "action": event.action,
        "sink_type": config.sink_type,
        "status": status,
        "attempts": attempts,
        # .hostname strips userinfo + port — never the secret path or credentials.
        "sink_host": urlsplit(config.webhook_url or "").hostname,
    }
    if error:
        after["error"] = error[:200]
    audit.write(
        session,
        actor_id=event.actor_id,
        action="notification.dispatch",
        source="app",
        object_ref=f"notifications/{event.action}",
        after=after,
        now=now(),
    )


def dispatch_notification(
    session: Session,
    event: NotificationEvent,
    *,
    client: httpx.Client,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = _MAX_ATTEMPTS,
) -> bool:
    """POST the content-free signal to the configured sink (opt-in, retried).

    Returns True on delivery; False if notifications are off/unconfigured or the
    signal was dead-lettered after exhausting retries. Best-effort: it never
    raises into the caller (a webhook failure must not undo an approve).
    """
    _now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))
    config = NotificationService(session, now=_now).config()
    if not config.deliverable:
        return False  # opt-in default off, or no sink configured — nothing leaves.

    payload = content_free_payload(event)
    body = _sink_body(config, payload)
    url = config.webhook_url
    assert url is not None  # deliverable ⇒ a URL is set
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(url, json=body, timeout=_TIMEOUT_S)
            if response.status_code < 400:
                _audit_dispatch(
                    session, event, config, status="delivered", attempts=attempt, now=_now
                )
                session.commit()
                return True
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        if attempt < max_attempts:
            sleep(min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** (attempt - 1))))

    # Exhausted: dead-letter the content-free payload (an operator signal) + audit.
    session.add(
        NotificationDeadLetter(
            action=event.action,
            sink_type=config.sink_type,
            payload=payload,
            attempts=max_attempts,
            last_error=last_error[:512],
            created_at=_now(),
        )
    )
    _audit_dispatch(
        session, event, config, status="failed", attempts=max_attempts, now=_now, error=last_error
    )
    session.commit()
    logger.warning("notification dead-lettered: %s (%s)", event.action, last_error)
    return False


def notify_in_background(
    engine: Engine,
    event: NotificationEvent,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> None:
    """Dispatch on a fresh session/client — the FastAPI BackgroundTasks entry point.

    Opens its own session (the request's is closed by the time this runs) and its
    own httpx client, and swallows everything: a notification is post-commit and
    best-effort, so nothing here can surface to the user.
    """
    factory = client_factory or httpx.Client
    try:
        with Session(engine) as session, factory() as client:
            dispatch_notification(session, event, client=client)
    except Exception:  # noqa: BLE001 - best-effort; never crash the worker thread
        logger.exception("notification dispatch crashed for %s", event.action)
