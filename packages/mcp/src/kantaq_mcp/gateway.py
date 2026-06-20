"""The per-call gateway: the eight checks, dispatch, audit (MOD-08, E09-T3).

Every tool call passes through ``Gateway.handle_call`` before any domain code
runs. The full v0.1 check sequence (FR-E09-3 — the eight checks, plus the
operational liveness/rate cuts):

1. identity — the request's verified actor is the actor the session was bound
   to (token validity itself is re-verified on every HTTP request). For a
   grant-derived session, the grant is **re-checked live** here too, so a
   revoked grant stops the session within the verifier budget (NFR-E06-2).
2. session liveness — a killed session stays dead until the agent re-inits.
3. expiry — sessions outlive nothing (a grant session expires with its grant);
   an expired session only denies.
4. rate limit — 50 calls/min, 500/session (PRD §15.1 defense 6); exceeding
   kills the session. Counted before the catalog checks so a runaway loop of
   garbage calls is cut off, not entertained.
5. collection scope — every collection the tool touches is inside the grant's
   resource scope (workspace-wide for token/v0.1 grants).
6. tool allowlist — fixed at session creation; unknown tools deny the same way.
7. verb match — the tool's required capability is one the grant authorized
   (re-checked against the grant verbs, independent of the cached allowlist).
8. write mode — by verb class: a propose-first verb (propose/comment) needs
   ``propose_only``; an *apply* verb (approve) needs ``direct_write``, which no
   v0.1 session holds (FR-E09-4, DEBT-08) — so approve is unreachable via the
   gateway for anyone (an over-scoped agent cannot self-approve, DEBT-37/D-33).
9. audit policy — the session carries a known audit policy; a call that cannot
   be audited per policy is refused (an agent action is never unaudited).

The eighth named check, **memory policy on reads**, is enforced inside the
memory-read tools via the session-derived ``ToolScope``: an entry the policy
excludes raises ``PolicyDenied``, which the gateway turns into an audited
``tool.deny`` (reason ``memory_policy``) — fail-closed, no existence leak.

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

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.identity import Role, TokenVerifier, VerifiedActor, verify_grant_row
from kantaq_core.memory_policy import policy_for
from kantaq_core.telemetry import TelemetryService
from kantaq_db.models import CapabilityGrantRow
from kantaq_mcp.catalog import APPLY_VERBS, CATALOG_BY_NAME, ToolSpec, dispatch
from kantaq_mcp.session import (
    DEFAULT_SESSION_TTL,
    KNOWN_AUDIT_POLICIES,
    WRITE_MODE_DIRECT_WRITE,
    WRITE_MODE_PROPOSE_ONLY,
    GatewaySession,
    SessionDerivationError,
    SessionRegistry,
    derive_session_from_grant,
)
from kantaq_mcp.tools import PolicyDenied, ToolScope
from kantaq_protocol import GRANT_EXPIRED
from kantaq_sync_engine.log import EventSigner

# How often aggregated agent reads are flushed to one summary row per agent.
DEFAULT_READ_FLUSH_INTERVAL = timedelta(seconds=60)

# Audit attribution when identity itself is what failed: there is no verified
# member to attribute to, and MOD-07 forbids defaulting silently — so the row
# says so explicitly.
UNKNOWN_ACTOR = "unknown"


def _read_payload_bytes(result: dict[str, Any]) -> int:
    """Wire size of a read result, for the MOD-08 payload tally (E26-T1 feed).

    The agent receives this dict as JSON, so its UTF-8 byte length is the input
    to ``metrics.summary``'s ``est_payload_bytes``/``est_tokens``. Measuring is
    best-effort observability — it must never fail a read — so a value the JSON
    encoder cannot serialize falls back to ``str`` and, failing even that, 0.
    """
    try:
        return len(json.dumps(result, default=str, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


# The 8-check deny vocabulary (FR-E09-3) plus the operational liveness/rate cuts.
DENY_IDENTITY = "identity"
DENY_COLLECTION_SCOPE = "collection_scope"
DENY_TOOL_ALLOWLIST = "tool_allowlist"
DENY_VERB_MATCH = "verb_match"
DENY_WRITE_MODE = "write_mode"
DENY_MEMORY_POLICY = "memory_policy"
DENY_EXPIRY = "expiry"
DENY_AUDIT_POLICY = "audit_policy"
DENY_RATE_LIMIT = "rate_limit"


@dataclass(frozen=True)
class GrantSessionRequest:
    """An agent's request to bind a session to a capability grant (E09-T3).

    Presented on the MCP connection (the ``mcp-grant-id`` / ``mcp-agent-role``
    headers) or to ``/v1/session/init``. ``agent_role`` is the optional context
    role (``code_agent`` …) that selects the memory policy applied to reads.
    """

    grant_id: str
    agent_role: str | None = None


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
        signer_for: Callable[[str], EventSigner | None] | None = None,
    ) -> None:
        self._engine = engine
        self.verifier = verifier or TokenVerifier(engine)
        self._raw_now = now or _default_now
        self.sessions = SessionRegistry(ttl=session_ttl)
        self._read_log = audit.AgentReadLog()
        self._read_flush_interval = read_flush_interval
        self._last_read_flush = self._now()
        # E04-T4: resolve the device signer for a member's write events. The
        # runtime injects this (keychain seed + the member's live self-grant)
        # only past the signing cutover; None here means events stay unsigned
        # (pre-cutover / tests) — the same default as the runtime's own writes.
        self._signer_for = signer_for

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

    def session_for(
        self,
        actor: VerifiedActor,
        *,
        session_id: str,
        grant_request: GrantSessionRequest | None = None,
    ) -> GatewaySession:
        """The gateway session for this transport id (token- or grant-derived).

        A session is created once and fixed: a returning ``session_id`` gets the
        same session (the model cannot escalate by changing headers mid-session).
        ``grant_request`` (when present, and only on first use) binds the session
        to a verified capability grant (FR-E09-2).
        """
        existing = self.sessions.get(session_id)
        if existing is not None:
            return existing
        if grant_request is not None:
            session = self._derive_grant_session(actor, session_id, grant_request)
        else:
            session = self.sessions.get_or_create(actor, session_id=session_id, now=self._now())
        # Telemetry (E28, opt-in no-op): repeat-session signal. The member
        # ULID only — never the token, scopes, or any call content.
        with Session(self._engine) as db:
            if TelemetryService(db, now=self._now).record(
                "mcp_session_started", {"member_id": session.member_id}
            ):
                db.commit()
        return session

    def _verify_and_derive(
        self, actor: VerifiedActor, session_id: str, request: GrantSessionRequest
    ) -> GatewaySession:
        """Verify the grant and derive a grant-scoped session (no registration).

        Fails closed with an audited denial: an unknown grant, a grant that is
        not the caller's, a forged/expired/revoked grant, or a bad agent role
        all refuse — nothing is created.
        """
        now = self._now()
        with Session(self._engine) as db:
            row = db.get(CapabilityGrantRow, request.grant_id)
            if row is None:
                self._deny_session_init(actor, DENY_IDENTITY, "no such grant")
            if row.subject != actor.member_id:
                self._deny_session_init(actor, DENY_IDENTITY, "grant does not belong to you")
            result = verify_grant_row(db, row, now=now)
            if not result.ok:
                reason = DENY_EXPIRY if result.reason == GRANT_EXPIRED else DENY_IDENTITY
                self._deny_session_init(actor, reason, f"grant rejected: {result.reason}")
            expires_at = _naive_utc(datetime.fromtimestamp(row.expires_at, UTC))
            try:
                return derive_session_from_grant(
                    actor,
                    session_id=session_id,
                    now=now,
                    grant_id=row.id,
                    resource=row.resource,
                    verbs=tuple(row.verbs),
                    expires_at=expires_at,
                    agent_role=request.agent_role,
                )
            except SessionDerivationError as exc:
                self._deny_session_init(actor, DENY_IDENTITY, str(exc))

    def _derive_grant_session(
        self, actor: VerifiedActor, session_id: str, request: GrantSessionRequest
    ) -> GatewaySession:
        """Verify, derive, and register a grant-scoped session for a transport id."""
        return self.sessions.put(self._verify_and_derive(actor, session_id, request))

    def describe_grant_session(
        self, actor: VerifiedActor, request: GrantSessionRequest
    ) -> dict[str, Any]:
        """The `/v1/session/init` descriptor: what session this grant yields.

        Verifies the grant and reports the session the agent will get — the
        allowlist, write mode, collection scope, expiry, and the headers to send
        on the MCP connection — without binding it to a transport id yet. The
        binding happens when the agent connects with those headers.
        """
        session = self._verify_and_derive(actor, "(preview)", request)
        return {
            "grant_id": session.grant_id,
            "agent_role": session.agent_role,
            "member_id": session.member_id,
            "collection_scope": list(session.collection_scope),
            "allowed_tools": list(session.allowed_tools),
            "write_mode": session.write_mode,
            "memory_policy_id": session.memory_policy_id,
            "audit_policy": session.audit_policy,
            "expires_at": session.expires_at.isoformat(),
            "connect_headers": {
                "mcp-grant-id": session.grant_id,
                **({"mcp-agent-role": session.agent_role} if session.agent_role else {}),
            },
        }

    def _deny_session_init(self, actor: VerifiedActor, reason: str, detail: str) -> NoReturn:
        self._write_denial(actor_id=actor.member_id, tool_name=None, reason=reason, detail=detail)
        raise GatewayDenied(reason, detail)

    def allowed_specs(self, session: GatewaySession) -> list[ToolSpec]:
        return [CATALOG_BY_NAME[name] for name in session.allowed_tools]

    def _tool_scope(self, session: GatewaySession, *, for_write: bool) -> ToolScope:
        """The scope handed to a tool: the agent role's memory policy + a signer.

        A role-less *agent* session carries ``is_agent`` with no policy, so the
        memory tools fail closed (an agent must declare a context role to read
        memory); a human session reads memory unfiltered. For a **write** verb,
        the device signer is resolved for the session's member (E04-T4) so the
        tool's emitted events are signed past the cutover; reads carry no signer.
        """
        is_agent = session.role == Role.agent.value
        signer = (
            self._signer_for(session.member_id)
            if for_write and self._signer_for is not None
            else None
        )
        if session.agent_role is None:
            return ToolScope(is_agent=is_agent, signer=signer)
        return ToolScope(
            agent_role=session.agent_role,
            memory_policy=policy_for(session.agent_role),
            is_agent=is_agent,
            signer=signer,
        )

    def _grant_live(self, grant_id: str, now: datetime) -> bool:
        """Re-check a derived session's grant for revocation/expiry (NFR-E06-2).

        A cheap per-call store read (the signature was verified at session
        creation and cannot change); revocation written by the runtime API is
        visible across the shared SQLite immediately, well inside the 5 s budget.
        """
        with Session(self._engine) as db:
            row = db.get(CapabilityGrantRow, grant_id)
            if row is None:
                return False
            return verify_grant_row(db, row, now=now).ok

    # ------------------------------------------------------------------ calls

    def handle_call(
        self,
        *,
        actor: VerifiedActor,
        session: GatewaySession,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the eight checks, then the tool. Raises GatewayDenied on failure."""
        now = self._now()

        # 1. identity — the token's actor is the session's, and (grant sessions)
        #    the grant is still live (revocation < 5 s, NFR-E06-2).
        if actor.member_id != session.member_id:
            self._deny(
                session,
                tool_name,
                DENY_IDENTITY,
                "the presented token does not belong to this session",
            )
        if session.grant_id is not None and not self._grant_live(session.grant_id, now):
            self._deny(
                session,
                tool_name,
                DENY_IDENTITY,
                "the session's grant was revoked or is no longer valid; re-initialize",
            )
        # 2. liveness + 3. expiry + 4. rate limit (the operational cuts).
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
        # Unknown tool: nothing to scope/verb-check against — deny as allowlist.
        spec = CATALOG_BY_NAME.get(tool_name)
        if spec is None:
            self._deny(session, tool_name, DENY_TOOL_ALLOWLIST, f"unknown tool {tool_name!r}")
        # 5. collection scope.
        if not session.permits_collections(spec.collections):
            self._deny(
                session,
                tool_name,
                DENY_COLLECTION_SCOPE,
                f"tool {tool_name!r} touches {list(spec.collections)} outside the "
                f"session's collection scope {list(session.collection_scope)}",
            )
        # 6. tool allowlist.
        if tool_name not in session.allowed_tools:
            self._deny(
                session,
                tool_name,
                DENY_TOOL_ALLOWLIST,
                f"tool {tool_name!r} is not in this session's allowlist",
            )
        # 7. verb match — the capability is one the grant authorized.
        if not session.permits_verb(spec.required_action):
            self._deny(
                session,
                tool_name,
                DENY_VERB_MATCH,
                f"the grant does not authorize {spec.required_action!r} for {tool_name!r}",
            )
        # 8. write mode — propose-first by verb class (FR-E09-4, DEBT-37/D-33).
        #    An APPLY verb (approve) mutates the canonical record directly, so it
        #    needs ``direct_write`` — which no v0.1 session holds (DEBT-08). So it
        #    is unreachable via the gateway for *anyone* (an over-scoped agent
        #    cannot self-approve; humans approve in the Inbox). A propose-first
        #    verb (propose/comment) needs ``propose_only``. This is the shared
        #    check, so the apply-verb block holds over HTTP *and* stdio.
        if spec.verb in APPLY_VERBS:
            if session.write_mode != WRITE_MODE_DIRECT_WRITE:
                self._deny(
                    session,
                    tool_name,
                    DENY_WRITE_MODE,
                    f"{spec.verb!r} applies a change to the canonical record and needs a "
                    "direct-write session; the gateway is propose-first — approve in the Inbox",
                )
        elif spec.verb != "read" and session.write_mode != WRITE_MODE_PROPOSE_ONLY:
            self._deny(
                session,
                tool_name,
                DENY_WRITE_MODE,
                f"session write mode {session.write_mode!r} does not allow {spec.verb!r}",
            )
        # audit policy — an agent action must be auditable per a known policy.
        if session.audit_policy not in KNOWN_AUDIT_POLICIES:
            self._deny(
                session,
                tool_name,
                DENY_AUDIT_POLICY,
                f"session audit policy {session.audit_policy!r} is unknown",
            )

        scope = self._tool_scope(session, for_write=spec.verb != "read")
        try:
            with Session(self._engine) as db:
                result = dispatch(
                    spec, db, actor_id=session.member_id, args=args, now=self._now, scope=scope
                )
        except PolicyDenied as denied:
            # The memory-policy check (check on reads): a withheld entry is a
            # denial, not a domain error — audited, fail-closed, no existence leak.
            self._deny(session, tool_name, DENY_MEMORY_POLICY, str(denied))

        if spec.verb == "read":
            self._read_log.record(
                session.member_id,
                object_ref=spec.read_ref(args) if spec.read_ref is not None else None,
                payload_bytes=_read_payload_bytes(result),
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
