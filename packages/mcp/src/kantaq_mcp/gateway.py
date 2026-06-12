"""The per-call gateway: checks, dispatch, audit (MOD-08, E09-T2).

Every tool call passes through ``Gateway.handle_call`` before any domain code
runs. The v0.0.5 check sequence (the minimal-session cut of the 8-check list,
FR-E09-3 — the grant-derived checks land in v0.1):

1. identity — the request's verified actor is the actor the session was bound
   to (token validity itself is re-verified on every HTTP request).
2. session liveness — a killed session stays dead until the agent re-inits.
3. expiry — sessions outlive nothing; an expired session only denies.
4. rate limit — 50 calls/min, 500/session (PRD §15.1 defense 6); exceeding
   kills the session. Counted before the allowlist so a runaway loop of
   garbage calls is cut off, not entertained.
5. tool allowlist — fixed at session creation; unknown tools deny the same way.
6. write mode — propose verbs need ``propose_only``; nothing grants
   ``direct_write`` in v0.0.5 (FR-E09-4).

A failed check applies nothing and writes a ``tool.deny`` audit row in its own
transaction (NFR-E09-1: the denial is atomic — there is no domain transaction
to taint because dispatch never started).

Audit policy (v0.0.5, per MOD-07's action vocabulary and PRD §8.6):
- agent *reads* aggregate — recorded on the ``AgentReadLog`` and flushed as
  one ``agent.read`` summary row per agent; the gateway owns the cadence
  (every ``read_flush_interval``, plus an explicit ``flush_reads`` at
  shutdown).
- agent *writes* are always detailed — ``agent_action_propose`` writes its
  ``proposal.create`` row inside the same transaction as the proposal itself.
- denials are always detailed — ``tool.deny`` with the failed check.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.identity import TokenVerifier, VerifiedActor
from kantaq_core.telemetry import TelemetryService
from kantaq_mcp.catalog import CATALOG_BY_NAME, ToolSpec, dispatch
from kantaq_mcp.session import (
    DEFAULT_SESSION_TTL,
    WRITE_MODE_PROPOSE_ONLY,
    GatewaySession,
    SessionRegistry,
)

# How often aggregated agent reads are flushed to one summary row per agent.
DEFAULT_READ_FLUSH_INTERVAL = timedelta(seconds=60)

# Audit attribution when identity itself is what failed: there is no verified
# member to attribute to, and MOD-07 forbids defaulting silently — so the row
# says so explicitly.
UNKNOWN_ACTOR = "unknown"

DENY_IDENTITY = "identity"
DENY_EXPIRY = "expiry"
DENY_RATE_LIMIT = "rate_limit"
DENY_TOOL_ALLOWLIST = "tool_allowlist"
DENY_WRITE_MODE = "write_mode"


class GatewayDenied(Exception):
    """A gateway check failed. Nothing was applied; the denial was audited."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _naive_utc(ts: datetime) -> datetime:
    """The store's encoding (naive UTC) — same rule as the tracker service."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def _default_now() -> datetime:
    return datetime.now(UTC)


class Gateway:
    """One per runtime process: sessions, checks, audit, dispatch."""

    def __init__(
        self,
        engine: Engine,
        *,
        verifier: TokenVerifier | None = None,
        now: Callable[[], datetime] | None = None,
        session_ttl: timedelta = DEFAULT_SESSION_TTL,
        read_flush_interval: timedelta = DEFAULT_READ_FLUSH_INTERVAL,
    ) -> None:
        self._engine = engine
        self.verifier = verifier or TokenVerifier(engine)
        self._raw_now = now or _default_now
        self.sessions = SessionRegistry(ttl=session_ttl)
        self._read_log = audit.AgentReadLog()
        self._read_flush_interval = read_flush_interval
        self._last_read_flush = self._now()

    def _now(self) -> datetime:
        return _naive_utc(self._raw_now())

    # ------------------------------------------------------------------- auth

    def authenticate(self, bearer: str | None) -> VerifiedActor | None:
        """Verify a presented bearer token; None is an identity failure."""
        if not bearer:
            return None
        return self.verifier.verify(bearer)

    def audit_identity_denial(self, *, detail: str) -> None:
        """An unauthenticated request: audited, attributed to no member."""
        self._write_denial(
            actor_id=UNKNOWN_ACTOR, tool_name=None, reason=DENY_IDENTITY, detail=detail
        )

    # ---------------------------------------------------------------- catalog

    def session_for(self, actor: VerifiedActor, *, session_id: str) -> GatewaySession:
        created = self.sessions.get(session_id) is None
        session = self.sessions.get_or_create(actor, session_id=session_id, now=self._now())
        if created:
            # Telemetry (E28, opt-in no-op): repeat-session signal. The member
            # ULID only — never the token, scopes, or any call content.
            with Session(self._engine) as db:
                if TelemetryService(db, now=self._now).record(
                    "mcp_session_started", {"member_id": session.member_id}
                ):
                    db.commit()
        return session

    def allowed_specs(self, session: GatewaySession) -> list[ToolSpec]:
        return [CATALOG_BY_NAME[name] for name in session.allowed_tools]

    # ------------------------------------------------------------------ calls

    def handle_call(
        self,
        *,
        actor: VerifiedActor,
        session: GatewaySession,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the checks, then the tool. Raises GatewayDenied on any failure."""
        now = self._now()

        if actor.member_id != session.member_id:
            self._deny(
                session,
                tool_name,
                DENY_IDENTITY,
                "the presented token does not belong to this session",
            )
        if session.killed:
            self._deny(
                session,
                tool_name,
                DENY_RATE_LIMIT,
                "session was terminated (rate limit); re-initialize to continue",
            )
        if session.expired(now):
            self._deny(session, tool_name, DENY_EXPIRY, "session expired; re-initialize")
        if not session.count_call(now):
            self._deny(
                session,
                tool_name,
                DENY_RATE_LIMIT,
                "rate limit exceeded (50/minute, 500/session); session terminated",
            )
        spec = CATALOG_BY_NAME.get(tool_name)
        if spec is None or tool_name not in session.allowed_tools:
            self._deny(
                session,
                tool_name,
                DENY_TOOL_ALLOWLIST,
                f"tool {tool_name!r} is not in this session's allowlist",
            )
        assert spec is not None  # narrowed by the allowlist check above
        if spec.verb != "read" and session.write_mode != WRITE_MODE_PROPOSE_ONLY:
            self._deny(
                session,
                tool_name,
                DENY_WRITE_MODE,
                f"session write mode {session.write_mode!r} does not allow {spec.verb!r}",
            )

        with Session(self._engine) as db:
            result = dispatch(spec, db, actor_id=session.member_id, args=args, now=self._now)

        if spec.verb == "read":
            self._read_log.record(
                session.member_id,
                object_ref=spec.read_ref(args) if spec.read_ref is not None else None,
            )
            self._flush_reads_if_due()
        return result

    # ------------------------------------------------------------------ audit

    def flush_reads(self) -> int:
        """Flush aggregated agent reads now; returns the rows written."""
        now = self._now()
        if self._read_log.pending == 0:
            self._last_read_flush = now
            return 0
        with Session(self._engine) as db:
            rows = self._read_log.flush(db, now=now)
            db.commit()
        self._last_read_flush = now
        return len(rows)

    def _flush_reads_if_due(self) -> None:
        if self._now() - self._last_read_flush >= self._read_flush_interval:
            self.flush_reads()

    def _deny(
        self, session: GatewaySession, tool_name: str | None, reason: str, message: str
    ) -> NoReturn:
        self._write_denial(
            actor_id=session.member_id,
            tool_name=tool_name,
            reason=reason,
            detail=message,
            session_id=session.session_id,
        )
        raise GatewayDenied(reason, message)

    def _write_denial(
        self,
        *,
        actor_id: str,
        tool_name: str | None,
        reason: str,
        detail: str,
        session_id: str | None = None,
    ) -> None:
        with Session(self._engine) as db:
            audit.write(
                db,
                actor_id=actor_id,
                action="tool.deny",
                source="mcp",
                object_ref=f"tools/{tool_name}" if tool_name else None,
                after={"reason": reason, "detail": detail, "session_id": session_id},
                now=self._now(),
            )
            db.commit()
