"""Opt-in, local-only telemetry (MOD-25 / Epic E28, D-10).

Default **off**: ``TelemetryService.record`` is a no-op until a Maintainer+
flips the toggle, and even then events go only to the local
``telemetry_events`` table — there is no remote collector (D-10); opted-in
users may *manually* share an export.

Content never leaks by construction, not by convention (FR-E28-1): every
event name must be registered in ``EVENTS`` with an exact set of allowed prop
keys, each typed numeric/categorical/id. An unregistered event, an
unregistered prop key, a wrong-typed value, or a string long enough to smuggle
prose all raise ``TelemetryError`` — there is no code path that stores a
ticket title, description, comment, or memory body. The privacy test pins
this with sentinel content.

The toggle itself is a human write, so flipping it writes an audit row
(``telemetry.enable`` / ``telemetry.disable``). Timestamps are injectable
(``now=``) for FakeClock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, Literal

from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_db.models import AgentProposal, LocalSetting, TelemetryEvent, Workspace

# The local_settings key holding the opt-in flag. Stored as "true"/"false";
# a missing row means the user never opted in (default off, FR-E28-1).
OPTIN_KEY = "telemetry.enabled"

# Categorical/id strings stay short: long free text is how content would
# sneak in. ULIDs are 26 chars; enum-ish values are shorter still.
_MAX_STR_LENGTH = 64

PropType = Literal["int", "float", "bool", "str"]

# The FR-E28-2 capture registry: event name -> {prop key: type}. Keys not
# listed here cannot be recorded, period. The conflict-rate and
# preview-before-approval events are deliberately absent until the seams that
# produce them exist (conflict surfacing and the proposal-preview panel land
# in later sprints) — never record a metric the product cannot yet mean.
EVENTS: dict[str, dict[str, PropType]] = {
    # An agent proposal was decided; seconds_to_decision feeds the
    # time-to-approve metric, the approve/reject split feeds acceptance rate.
    "proposal_approved": {"seconds_to_decision": "float"},
    "proposal_rejected": {"seconds_to_decision": "float"},
    # The Inbox (or an API consumer) listed proposals — queue engagement.
    "proposals_listed": {"count": "int"},
    # A new MCP gateway session was derived; member_id (a ULID, never an
    # email) feeds the repeat-sessions metric.
    "mcp_session_started": {"member_id": "str"},
    # A ticket's activity (audit) feed was read — audit-query frequency.
    "activity_viewed": {"count": "int"},
}


class TelemetryError(ValueError):
    """A record() call broke the registry contract (programmer error)."""


@dataclass(frozen=True)
class TelemetryMetrics:
    """The FR-E28-2 outcome metrics, computed from local data on demand."""

    enabled: bool
    events_total: int
    proposal_acceptance_rate: float | None
    median_seconds_to_approve: float | None
    mcp_sessions_total: int
    repeat_session_members: int
    activity_views_total: int
    install_to_first_proposal_seconds: float | None
    weekly_active: bool


def _utcnow() -> datetime:
    # Naive UTC — the store's one timestamp encoding (MOD-03 rule).
    return datetime.now(UTC).replace(tzinfo=None)


def _check_prop(name: str, key: str, value: Any, expected: PropType) -> None:
    ok = (
        (expected == "int" and isinstance(value, int) and not isinstance(value, bool))
        or (expected == "float" and isinstance(value, int | float) and not isinstance(value, bool))
        or (expected == "bool" and isinstance(value, bool))
        or (expected == "str" and isinstance(value, str))
    )
    if not ok:
        raise TelemetryError(
            f"telemetry event {name!r} prop {key!r} must be {expected}, got {type(value).__name__}"
        )
    if expected == "str":
        text = str(value)
        if len(text) > _MAX_STR_LENGTH or "\n" in text:
            raise TelemetryError(
                f"telemetry event {name!r} prop {key!r} exceeds the categorical bound "
                f"({_MAX_STR_LENGTH} chars, single line) — free text is not recordable"
            )


class TelemetryService:
    """Toggle, capture, and inspection over one SQLModel session.

    The session (and so the transaction boundary) belongs to the caller:
    a capture site that records inside a domain write commits or rolls back
    with it.
    """

    def __init__(self, session: Session, *, now: Callable[[], datetime] | None = None) -> None:
        self._session = session
        self._now = now or _utcnow

    # ------------------------------------------------------------------ toggle

    def enabled(self) -> bool:
        row = self._session.get(LocalSetting, OPTIN_KEY)
        return row is not None and row.value == "true"

    def set_enabled(self, value: bool, *, actor_id: str) -> bool:
        """Flip the opt-in flag; a human write, so it writes an audit row."""
        before = self.enabled()
        if before == value:
            return value
        ts = self._now()
        row = self._session.get(LocalSetting, OPTIN_KEY)
        if row is None:
            row = LocalSetting(key=OPTIN_KEY, value="true" if value else "false", updated_at=ts)
            self._session.add(row)
        else:
            row.value = "true" if value else "false"
            row.updated_at = ts
            self._session.add(row)
        audit.write(
            self._session,
            actor_id=actor_id,
            action="telemetry.enable" if value else "telemetry.disable",
            source="app",
            object_ref=f"local_settings/{OPTIN_KEY}",
            before={"enabled": before},
            after={"enabled": value},
            now=ts,
        )
        return value

    # ----------------------------------------------------------------- capture

    def record(self, name: str, props: Mapping[str, Any] | None = None) -> bool:
        """Store one event if opted in; returns whether a row was written.

        The registry is enforced before the opt-in check, so a capture-site
        bug fails tests even when the suite never opts in.
        """
        spec = EVENTS.get(name)
        if spec is None:
            raise TelemetryError(f"unregistered telemetry event: {name!r}")
        supplied = dict(props or {})
        for key, value in supplied.items():
            expected = spec.get(key)
            if expected is None:
                raise TelemetryError(f"telemetry event {name!r} does not allow prop {key!r}")
            _check_prop(name, key, value, expected)
        if not self.enabled():
            return False
        self._session.add(TelemetryEvent(name=name, props=supplied, created_at=self._now()))
        return True

    # -------------------------------------------------------------- inspection

    def events(self, *, limit: int = 200) -> list[TelemetryEvent]:
        """Newest first, bounded — the inspection view's raw feed (FR-E28-3)."""
        statement = (
            select(TelemetryEvent).order_by(col(TelemetryEvent.id).desc()).limit(max(0, limit))
        )
        return list(self._session.exec(statement).all())

    def metrics(self) -> TelemetryMetrics:
        """Fold the captured events (plus two domain facts) into the outcome metrics.

        install→first-proposal derives from rows the tracker already holds
        (workspace and proposal timestamps — no content), so it is available
        even when capture was enabled late.
        """
        rows = self._session.exec(select(TelemetryEvent)).all()
        approved = [r for r in rows if r.name == "proposal_approved"]
        rejected = [r for r in rows if r.name == "proposal_rejected"]
        decided = len(approved) + len(rejected)
        durations = [
            float(r.props["seconds_to_decision"])
            for r in approved + rejected
            if isinstance(r.props.get("seconds_to_decision"), int | float)
        ]
        session_members = [
            str(r.props.get("member_id", "")) for r in rows if r.name == "mcp_session_started"
        ]
        repeat = sum(1 for m in set(session_members) if m and session_members.count(m) > 1)

        first_workspace = self._session.exec(
            select(Workspace).order_by(col(Workspace.created_at)).limit(1)
        ).first()
        first_proposal = self._session.exec(
            select(AgentProposal).order_by(col(AgentProposal.created_at)).limit(1)
        ).first()
        install_to_first: float | None = None
        if first_workspace is not None and first_proposal is not None:
            install_to_first = (
                first_proposal.created_at - first_workspace.created_at
            ).total_seconds()

        week_ago = self._now() - timedelta(days=7)
        return TelemetryMetrics(
            enabled=self.enabled(),
            events_total=len(rows),
            proposal_acceptance_rate=(len(approved) / decided) if decided else None,
            median_seconds_to_approve=median(durations) if durations else None,
            mcp_sessions_total=len(session_members),
            repeat_session_members=repeat,
            activity_views_total=sum(1 for r in rows if r.name == "activity_viewed"),
            install_to_first_proposal_seconds=install_to_first,
            weekly_active=any(r.created_at >= week_ago for r in rows),
        )
