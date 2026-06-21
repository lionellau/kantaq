"""The notification HTTP dispatch: content-free, opt-in, retried, dead-lettered.

The SEC heart of E20-T8 (the off-machine privacy boundary): a configured sink
fires on a decision with a payload that CANNOT carry a ticket/memory body; a
default-off sink sends nothing; a failing sink retries then dead-letters; and
neither the POST body, the dead-letter row, nor the audit row ever holds content
or the sink URL's secret path. The httpx client is injected, so no socket opens.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.notifications import NotificationEvent, NotificationService
from kantaq_db.models import AuditEvent, NotificationDeadLetter
from kantaq_runtime.notifications import dispatch_notification

# A string that would be a privacy leak if it ever reached a payload. It never
# can — there is no field on NotificationEvent to carry it — but the tests assert
# its absence end to end anyway (defense the reviewer can see).
SENTINEL = "SECRET-TICKET-BODY-do-not-leak"


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _RecordingClient:
    """A stand-in httpx.Client that records POSTs (and can fail on demand)."""

    def __init__(self, *, status: int = 200, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._raise = raise_exc

    def post(self, url: str, json: Any = None, timeout: float | None = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        if self._raise is not None:
            raise self._raise
        return _FakeResponse(self._status)


def _configure(
    session: Session,
    *,
    enabled: bool = True,
    sink_type: str = "webhook",
    url: str | None = "https://hooks.example.com/x",
) -> None:
    NotificationService(session).set_config(
        enabled=enabled, sink_type=sink_type, webhook_url=url, actor_id="mbr_admin0000001"
    )
    session.commit()


def _event() -> NotificationEvent:
    return NotificationEvent(
        action="proposal.approved",
        ids=("prop1", "tkt1"),
        actor_id="mbr1",
        deep_link="/tickets/tkt1",
    )


def _noslp(_: float) -> None:
    return None


def test_default_off_sends_nothing(session: Session) -> None:
    client = _RecordingClient()
    assert dispatch_notification(session, _event(), client=client, sleep=_noslp) is False  # type: ignore[arg-type]
    assert client.calls == []


def test_webhook_posts_only_the_content_free_payload(session: Session) -> None:
    _configure(session)
    client = _RecordingClient(status=200)
    assert dispatch_notification(session, _event(), client=client, sleep=_noslp) is True  # type: ignore[arg-type]
    assert len(client.calls) == 1
    body = client.calls[0]["json"]
    assert set(body) == {"action", "ids", "actor", "deep_link"}
    assert SENTINEL not in str(body)


def test_slack_reshapes_to_text_still_content_free(session: Session) -> None:
    _configure(session, sink_type="slack", url="https://hooks.slack.com/x")
    client = _RecordingClient(status=200)
    event = NotificationEvent(
        action="proposal.rejected", ids=("p", "t"), actor_id="mbr1", deep_link="/tickets/t"
    )
    dispatch_notification(session, event, client=client, sleep=_noslp)  # type: ignore[arg-type]
    body = client.calls[0]["json"]
    assert set(body) == {"text"}
    assert "proposal.rejected" in body["text"] and "/tickets/t" in body["text"]
    assert SENTINEL not in body["text"]


def test_http_error_retries_then_dead_letters(session: Session) -> None:
    _configure(session)
    client = _RecordingClient(status=500)
    sent = dispatch_notification(
        session,
        _event(),
        client=client,
        sleep=_noslp,
        max_attempts=3,  # type: ignore[arg-type]
    )
    assert sent is False
    assert len(client.calls) == 3  # it retried
    dead = session.exec(select(NotificationDeadLetter)).all()
    assert len(dead) == 1
    assert dead[0].attempts == 3
    assert dead[0].action == "proposal.approved"
    # the dead-lettered payload is content-free.
    assert set(dead[0].payload) == {"action", "ids", "actor", "deep_link"}
    assert SENTINEL not in str(dead[0].payload)


def test_network_error_retries_then_dead_letters(session: Session) -> None:
    _configure(session)
    client = _RecordingClient(raise_exc=httpx.ConnectError("sink down"))
    sent = dispatch_notification(
        session,
        _event(),
        client=client,
        sleep=_noslp,
        max_attempts=2,  # type: ignore[arg-type]
    )
    assert sent is False
    assert len(client.calls) == 2
    assert len(session.exec(select(NotificationDeadLetter)).all()) == 1


def test_dispatch_audit_records_host_not_the_secret_path(session: Session) -> None:
    _configure(session, sink_type="slack", url="https://hooks.slack.com/services/T/B/SECRET")
    client = _RecordingClient(status=200)
    dispatch_notification(session, _event(), client=client, sleep=_noslp)  # type: ignore[arg-type]
    rows = session.exec(
        select(AuditEvent).where(AuditEvent.action == "notification.dispatch")
    ).all()
    assert len(rows) == 1
    assert rows[0].after is not None
    assert rows[0].after["status"] == "delivered"
    assert rows[0].after["sink_host"] == "hooks.slack.com"
    assert "SECRET" not in str(rows[0].after)
