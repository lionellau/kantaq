"""The notification core: content-free payload + the audited sink config (E20-T8).

The privacy boundary, proven structurally: a :class:`NotificationEvent` has no
body field, ``content_free_payload`` emits exactly four keys, and the config
toggle is opt-in default-off + audited (host only, never the secret URL path).
The HTTP dispatch (retries, dead-letter) is tested in the runtime suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.notifications import (
    NotificationConfigError,
    NotificationEvent,
    NotificationService,
    content_free_payload,
)
from kantaq_db.models import AuditEvent

ACTOR = "mbr_notif00000001"


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


def test_payload_is_exactly_the_four_content_free_keys() -> None:
    event = NotificationEvent(
        action="proposal.approved", ids=("p1", "t1"), actor_id="m1", deep_link="/tickets/t1"
    )
    payload = content_free_payload(event)
    assert payload == {
        "action": "proposal.approved",
        "ids": ["p1", "t1"],
        "actor": "m1",
        "deep_link": "/tickets/t1",
    }


def test_event_has_no_body_field() -> None:
    # The dataclass fields are exactly the content-free four — there is no slot
    # for a ticket/memory body, so a content leak is structurally impossible.
    names = {f.name for f in fields(NotificationEvent)}
    assert names == {"action", "ids", "actor_id", "deep_link"}


def test_event_rejects_an_unknown_action() -> None:
    with pytest.raises(ValueError, match="unknown notification action"):
        NotificationEvent(action="ticket.deleted", ids=(), actor_id="m", deep_link="/")


def test_config_is_opt_in_default_off(session: Session) -> None:
    config = NotificationService(session).config()
    assert config.enabled is False
    assert config.deliverable is False


def test_set_config_persists_and_audits_host_only(session: Session) -> None:
    config = NotificationService(session).set_config(
        enabled=True,
        sink_type="webhook",
        webhook_url="https://hooks.example.com/abc/SECRET-TOKEN",
        actor_id=ACTOR,
    )
    session.commit()
    assert config.enabled and config.deliverable and config.sink_type == "webhook"
    rows = session.exec(select(AuditEvent).where(AuditEvent.action == "notification.enable")).all()
    assert len(rows) == 1
    # The audit row records the sink HOST, never the secret path.
    assert rows[0].after is not None
    assert rows[0].after["sink_host"] == "hooks.example.com"
    assert "SECRET-TOKEN" not in str(rows[0].after)


def test_enabling_without_a_sink_url_fails_closed(session: Session) -> None:
    with pytest.raises(NotificationConfigError, match="without a sink URL"):
        NotificationService(session).set_config(
            enabled=True, sink_type="webhook", webhook_url=None, actor_id=ACTOR
        )


def test_unknown_sink_type_is_rejected(session: Session) -> None:
    with pytest.raises(NotificationConfigError, match="unknown sink type"):
        NotificationService(session).set_config(
            enabled=False, sink_type="carrier_pigeon", webhook_url=None, actor_id=ACTOR
        )


def test_a_non_http_url_is_rejected(session: Session) -> None:
    with pytest.raises(NotificationConfigError, match="absolute http"):
        NotificationService(session).set_config(
            enabled=False, sink_type="webhook", webhook_url="file:///etc/passwd", actor_id=ACTOR
        )


def test_a_url_with_embedded_credentials_is_rejected(session: Session) -> None:
    # SEC review (MED): userinfo in the URL would otherwise ride into the audit
    # row + the GET response via netloc — reject it at the source.
    with pytest.raises(NotificationConfigError, match="must not embed credentials"):
        NotificationService(session).set_config(
            enabled=False,
            sink_type="webhook",
            webhook_url="https://user:SECRET@hooks.example.com/x",
            actor_id=ACTOR,
        )


def test_disable_keeps_the_url_but_stops_delivery(session: Session) -> None:
    svc = NotificationService(session)
    svc.set_config(
        enabled=True, sink_type="webhook", webhook_url="https://hooks.example.com/x", actor_id=ACTOR
    )
    session.commit()
    config = svc.set_config(
        enabled=False,
        sink_type="webhook",
        webhook_url="https://hooks.example.com/x",
        actor_id=ACTOR,
    )
    session.commit()
    assert config.enabled is False
    assert config.deliverable is False  # off ⇒ nothing leaves, even with a URL set
