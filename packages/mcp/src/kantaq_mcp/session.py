"""Minimal gateway sessions bound to a member token (E09-T2, FR-E09-1).

A ``GatewaySession`` is the v0.0.5 cut of the PRD §6.9 MCPSession: it is
derived from a verified member token (humans: role matrix; agents: token
scopes), pins the tool allowlist and write mode at creation (PRD §15.1
defense 1), expires, and carries the rate-limit counters. The full
grant-derived session (collection scope, memory policy, audit policy fields)
lands in v0.1 with capability grants (FR-E09-2).

Sessions are keyed by the streamable-HTTP transport's ``mcp-session-id``: an
expired or rate-limited session keeps denying until the agent re-initializes,
which creates a fresh transport session and so a fresh gateway session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kantaq_core.identity import Action, VerifiedActor, can
from kantaq_mcp.catalog import CATALOG

# v0.0.5 session lifetime (PRD: agent grants short-lived, 1 h default).
DEFAULT_SESSION_TTL = timedelta(hours=1)

# Rate limits per PRD §15.1 defense 6: exceeding either kills the session.
RATE_LIMIT_PER_MINUTE = 50
RATE_LIMIT_PER_SESSION = 500
_RATE_WINDOW = timedelta(minutes=1)

# v0.0.5 write modes. ``direct_write`` exists in the protocol but no v0.0.5
# session can hold it (DEBT-08: graduation is undecided, propose-first rules).
WriteMode = str
WRITE_MODE_READ_ONLY: WriteMode = "read_only"
WRITE_MODE_PROPOSE_ONLY: WriteMode = "propose_only"


@dataclass
class GatewaySession:
    """One agent connection's scope, expiry, and rate state."""

    session_id: str
    member_id: str
    role: str
    token_id: str
    scopes: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    write_mode: WriteMode
    created_at: datetime
    expires_at: datetime
    calls_total: int = 0
    window_start: datetime | None = None
    window_calls: int = 0
    killed: bool = False

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def count_call(self, now: datetime) -> bool:
        """Record one call against the limits; False kills the session.

        Fixed one-minute windows: deterministic under FakeClock and within a
        factor of two of a sliding window, which is plenty for a cutoff whose
        job is stopping a runaway agent, not precise metering.
        """
        if self.window_start is None or now - self.window_start >= _RATE_WINDOW:
            self.window_start = now
            self.window_calls = 0
        self.window_calls += 1
        self.calls_total += 1
        if self.window_calls > RATE_LIMIT_PER_MINUTE or self.calls_total > RATE_LIMIT_PER_SESSION:
            self.killed = True
            return False
        return True


def derive_session(
    actor: VerifiedActor,
    *,
    session_id: str,
    now: datetime,
    ttl: timedelta = DEFAULT_SESSION_TTL,
) -> GatewaySession:
    """Turn a verified member token into a scoped session (FR-E09-1).

    The allowlist is the catalog filtered by what the actor may do — the
    identity matrix for human roles, token scopes for agents (``can`` fails
    closed on anything unknown). The write mode is ``propose_only`` when the
    actor may propose, else ``read_only``; nothing in v0.0.5 grants
    ``direct_write`` (FR-E09-4 propose-first default).
    """
    allowed = tuple(
        spec.name
        for spec in CATALOG
        if can(actor.role, Action(spec.required_action), scopes=list(actor.scopes))
    )
    proposes = can(actor.role, Action.proposals_write, scopes=list(actor.scopes))
    return GatewaySession(
        session_id=session_id,
        member_id=actor.member_id,
        role=actor.role,
        token_id=actor.token_id,
        scopes=actor.scopes,
        allowed_tools=allowed,
        write_mode=WRITE_MODE_PROPOSE_ONLY if proposes else WRITE_MODE_READ_ONLY,
        created_at=now,
        expires_at=now + ttl,
    )


@dataclass
class SessionRegistry:
    """Gateway sessions keyed by transport session id, pruned on creation."""

    ttl: timedelta = DEFAULT_SESSION_TTL
    _sessions: dict[str, GatewaySession] = field(default_factory=dict)

    def get_or_create(
        self, actor: VerifiedActor, *, session_id: str, now: datetime
    ) -> GatewaySession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        self._prune(now)
        created = derive_session(actor, session_id=session_id, now=now, ttl=self.ttl)
        self._sessions[session_id] = created
        return created

    def get(self, session_id: str) -> GatewaySession | None:
        return self._sessions.get(session_id)

    def _prune(self, now: datetime) -> None:
        # Dead sessions still deny correctly (expiry/killed are re-checked per
        # call), so pruning is purely a memory bound: drop sessions a full TTL
        # past their expiry — long after any transport session id stops
        # arriving for them.
        cutoff = [
            sid for sid, session in self._sessions.items() if now - session.expires_at >= self.ttl
        ]
        for sid in cutoff:
            del self._sessions[sid]
