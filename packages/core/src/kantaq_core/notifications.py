"""Outbound, content-free, opt-in notifications (E20-T8 / MOD-12 / PRD §16.10).

The async loop the persona study asked for: when a proposal is approved or
rejected (or a sync conflict is minted), a remote teammate learns it WITHOUT
polling the Inbox. The signal is deliberately minimal and privacy-bounded — the
MOD-25 telemetry discipline applied to dispatch:

* **Content-free by construction.** A :class:`NotificationEvent` can carry ONLY
  ids + the action + the actor + a deep-link. There is no field for a ticket or
  memory body, so a content leak is *structurally impossible*, not merely
  reviewed away. :func:`content_free_payload` is the only payload builder and it
  emits exactly those keys.
* **Opt-in, default off.** No signal leaves the machine until a maintainer turns
  it on. The config lives in ``local_settings`` (per-machine, never synced —
  like the telemetry opt-in), so a sink URL never enters the sync stream.
* **Never widens permission.** A notification is a read-shaped side effect; it
  grants nothing and carries no token. Configuring it is a Maintainer+ human
  write (the runtime gates the API); an agent can never enable it or change the
  sink.

The HTTP dispatch itself (retries, dead-letter) lives in the runtime
(``kantaq_runtime.notifications``); this module is pure: the event, the
content-free payload, and the config service (no httpx, no I/O).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session

from kantaq_core import audit
from kantaq_db.models import LocalSetting

# The trigger set (PRD §16.10): a proposal decided, or a sync conflict minted.
# NOT every sync event — the signal is for the propose→approve loop + conflicts.
NOTIFICATION_ACTIONS: tuple[str, ...] = (
    "proposal.approved",
    "proposal.rejected",
    "conflict.minted",
)

# The sink transports v0.3 ships: a generic webhook (the floor — POST the raw
# content-free JSON) and Slack (the same payload re-shaped as Slack's {"text"}).
# Email stays a Sprint 10+ convenience (it needs an SMTP surface); it is
# deliberately not offered here so the config can never name an unbuilt sink.
SINK_TYPES: tuple[str, ...] = ("webhook", "slack")

NOTIFY_ENABLED_KEY = "notification.enabled"
NOTIFY_SINK_TYPE_KEY = "notification.sink_type"
NOTIFY_WEBHOOK_URL_KEY = "notification.webhook_url"


@dataclass(frozen=True)
class NotificationEvent:
    """One outbound, content-free signal.

    ``ids`` are ULIDs (e.g. the proposal + its ticket, or the conflict record);
    ``deep_link`` is a RELATIVE app path (``/tickets/<id>``), never an absolute
    URL carrying a host or a query secret. There is intentionally no body field.
    """

    action: str
    ids: tuple[str, ...]
    actor_id: str
    deep_link: str

    def __post_init__(self) -> None:
        if self.action not in NOTIFICATION_ACTIONS:
            raise ValueError(f"unknown notification action {self.action!r}")


def content_free_payload(event: NotificationEvent) -> dict[str, Any]:
    """The exact JSON a webhook receives — ids + action + actor + deep-link only.

    This is the ONLY payload builder; nothing here can ever reach a ticket or
    memory body (there is no parameter for one). The Slack re-shaping in the
    runtime is derived from these same four fields.
    """
    return {
        "action": event.action,
        "ids": list(event.ids),
        "actor": event.actor_id,
        "deep_link": event.deep_link,
    }


@dataclass(frozen=True)
class NotificationConfig:
    """The per-machine notification sink config (read from ``local_settings``)."""

    enabled: bool
    sink_type: str
    webhook_url: str | None

    @property
    def deliverable(self) -> bool:
        """True only when a signal can actually go out (opt-in + a sink set)."""
        return self.enabled and self.webhook_url is not None


class NotificationConfigError(Exception):
    """A rejected config change (bad sink type or URL); nothing is written."""


def _validate_webhook_url(url: str) -> str:
    """A sink URL must be an absolute http(s) URL with a host (fail closed).

    The scheme allowlist keeps a misconfiguration from pointing the dispatcher at
    ``file://`` or some odd scheme; the host requirement rejects a bare path.
    Embedded credentials (``user:pass@host``) are rejected so a secret can never
    ride the URL into an audit row or the GET response (SEC review). The URL is a
    Maintainer-set value, so SSRF to an arbitrary internal host is an accepted,
    documented risk (the maintainer is trusted to point at their own sink), not
    an authz boundary this function enforces.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotificationConfigError(
            "the webhook URL must be an absolute http(s) URL (e.g. https://hooks.example.com/…)"
        )
    if parts.username or parts.password:
        raise NotificationConfigError(
            "the webhook URL must not embed credentials (user:pass@host); the token belongs in "
            "the URL path or the sink's own auth, not the userinfo"
        )
    return url


class NotificationService:
    """Read/write the notification sink config, audited like the telemetry toggle.

    Config is per-machine ``local_settings`` (never synced); enabling it or
    changing the sink writes an audit row, so a sink change is attributable. The
    runtime gates these calls behind a Maintainer+ human action — this service
    never checks permission itself (it is pure over the gateway/API).
    """

    def __init__(self, session: Session, *, now: Any = None) -> None:
        self._session = session
        self._now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))

    def _get(self, key: str) -> str | None:
        row = self._session.get(LocalSetting, key)
        return row.value if row is not None else None

    def _set(self, key: str, value: str) -> None:
        ts = self._now()
        row = self._session.get(LocalSetting, key)
        if row is None:
            self._session.add(LocalSetting(key=key, value=value, updated_at=ts))
        else:
            row.value = value
            row.updated_at = ts
            self._session.add(row)

    def config(self) -> NotificationConfig:
        sink_type = self._get(NOTIFY_SINK_TYPE_KEY) or "webhook"
        return NotificationConfig(
            enabled=self._get(NOTIFY_ENABLED_KEY) == "true",
            sink_type=sink_type if sink_type in SINK_TYPES else "webhook",
            webhook_url=self._get(NOTIFY_WEBHOOK_URL_KEY) or None,
        )

    def set_config(
        self,
        *,
        enabled: bool,
        sink_type: str,
        webhook_url: str | None,
        actor_id: str,
    ) -> NotificationConfig:
        """Validate + persist the sink config; writes an audit row (human write).

        Enabling without a sink URL is rejected (fail closed — an enabled-but-
        unconfigured sink would silently drop every signal).
        """
        if sink_type not in SINK_TYPES:
            raise NotificationConfigError(
                f"unknown sink type {sink_type!r}; expected one of {SINK_TYPES}"
            )
        url = _validate_webhook_url(webhook_url) if webhook_url else None
        if enabled and url is None:
            raise NotificationConfigError("cannot enable notifications without a sink URL")

        before = self.config()
        self._set(NOTIFY_ENABLED_KEY, "true" if enabled else "false")
        self._set(NOTIFY_SINK_TYPE_KEY, sink_type)
        if url is not None:
            self._set(NOTIFY_WEBHOOK_URL_KEY, url)
        ts = self._now()
        # The audit row records the decision + the sink HOST only — never the full
        # URL (a Slack webhook path carries a secret token), and never a body.
        audit.write(
            self._session,
            actor_id=actor_id,
            action="notification.enable" if enabled else "notification.disable",
            source="app",
            object_ref=f"local_settings/{NOTIFY_ENABLED_KEY}",
            before={"enabled": before.enabled, "sink_type": before.sink_type},
            after={
                "enabled": enabled,
                "sink_type": sink_type,
                # .hostname strips userinfo + port — never the secret path or creds.
                "sink_host": urlsplit(url).hostname if url else None,
            },
            now=ts,
        )
        return self.config()


__all__ = [
    "NOTIFICATION_ACTIONS",
    "NOTIFY_ENABLED_KEY",
    "NOTIFY_SINK_TYPE_KEY",
    "NOTIFY_WEBHOOK_URL_KEY",
    "SINK_TYPES",
    "NotificationConfig",
    "NotificationConfigError",
    "NotificationEvent",
    "NotificationService",
    "content_free_payload",
]
