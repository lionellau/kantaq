"""Gateway sessions: minimal (token) and grant-derived (E09-T2 → E09-T3).

A ``GatewaySession`` is the runtime form of the PRD §6.9 MCPSession. There are
two ways to derive one, sharing the same shape and the same per-call checks:

* **Token-derived (v0.0.5, :func:`derive_session`)** — the minimal session: a
  verified member token gives the tool allowlist (catalog ∩ what the actor's
  role/scopes allow), the write mode, and a 1 h expiry. Collection scope is the
  whole workspace, there is no agent context role, and the audit policy is the
  standard one. This is the fallback an agent gets by presenting only a member
  token (``kantaq mcp dev``).

* **Grant-derived (v0.1, :func:`derive_session_from_grant`, FR-E09-2)** — the
  full session: the capability grant (MOD-06) supplies the *verbs* (the tool
  allowlist and write mode narrow to exactly what the grant authorized — a grant
  never widens the role, D-03), the *collection scope* (from the grant resource),
  the *expiry* (the grant's own ``expires_at``), and — paired with an agent
  **context role** (``code_agent`` …) — the *memory policy* applied to reads.

Both feed :meth:`Gateway.handle_call`'s eight checks (FR-E09-3): identity,
collection scope, tool allowlist, verb match, write mode, memory policy on reads,
expiry, audit policy. Sessions are keyed by the streamable-HTTP transport's
``mcp-session-id``; an expired, killed, or grant-revoked session keeps denying
until the agent re-initializes (a fresh transport session → a fresh derivation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kantaq_core.identity import Action, VerifiedActor, can
from kantaq_core.memory_policy import is_agent_role, policy_for
from kantaq_mcp.catalog import CATALOG

# v0.0.5 session lifetime (PRD: agent grants short-lived, 1 h default).
DEFAULT_SESSION_TTL = timedelta(hours=1)

# Rate limits per PRD §15.1 defense 6: exceeding either kills the session.
RATE_LIMIT_PER_MINUTE = 50
RATE_LIMIT_PER_SESSION = 500
_RATE_WINDOW = timedelta(minutes=1)

# v0.0.5 write modes. ``direct_write`` exists in the protocol but no v0.1
# session can hold it (DEBT-08: graduation is undecided, propose-first rules).
WriteMode = str
WRITE_MODE_READ_ONLY: WriteMode = "read_only"
WRITE_MODE_PROPOSE_ONLY: WriteMode = "propose_only"

# The action a grant must carry for a session to be allowed to propose writes.
PROPOSALS_WRITE = Action.proposals_write.value

# Collection scope: a token session (or a workspace-wide grant) sees every
# collection its verbs allow; ``"*"`` is that wildcard. A narrower grant resource
# (a future shape-filtered grant, DEBT-12) lists specific collections instead.
COLLECTION_SCOPE_ALL = "*"

# Audit policy (v0.1: one policy — reads aggregate, writes + denials detail, per
# MOD-07 §8.6). The session carries it so check 8 can fail closed on an unknown
# policy; user-defined audit policies are a later release.
AUDIT_POLICY_STANDARD = "standard"
KNOWN_AUDIT_POLICIES = frozenset({AUDIT_POLICY_STANDARD})


def _collection_scope_from_resource(resource: str) -> tuple[str, ...]:
    """Map a grant ``resource`` to the collections the session may touch.

    v0.1 grants are workspace-wide (``workspace/main``) → every collection
    (``("*",)``). A resource that names collections (comma/space separated, the
    shape-filtered-grant shape reserved for v0.2/DEBT-12) scopes to exactly those.
    """
    head = resource.split("/", 1)[0].strip().lower()
    if head in {"workspace", "", COLLECTION_SCOPE_ALL}:
        return (COLLECTION_SCOPE_ALL,)
    parts = [token for token in resource.replace(",", " ").split() if token]
    return tuple(parts) if parts else (COLLECTION_SCOPE_ALL,)


@dataclass
class GatewaySession:
    """One agent connection's scope, expiry, and rate state.

    The grant-derived fields (``collection_scope``, ``granted_verbs``,
    ``agent_role``, ``memory_policy_id``, ``audit_policy``, ``grant_id``) carry
    PRD §6.9's full session shape; a token-derived session fills them with the
    workspace-wide, no-agent-role, standard-audit defaults.
    """

    session_id: str
    member_id: str
    role: str
    token_id: str
    scopes: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    write_mode: WriteMode
    created_at: datetime
    expires_at: datetime
    # Grant-derived session shape (FR-E09-2); permissive defaults for the
    # token-derived minimal session.
    collection_scope: tuple[str, ...] = (COLLECTION_SCOPE_ALL,)
    granted_verbs: tuple[str, ...] = ()
    agent_role: str | None = None
    memory_policy_id: str | None = None
    audit_policy: str = AUDIT_POLICY_STANDARD
    grant_id: str | None = None
    calls_total: int = 0
    window_start: datetime | None = None
    window_calls: int = 0
    killed: bool = False

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def permits_collections(self, collections: tuple[str, ...]) -> bool:
        """Check 2: every collection a tool touches is inside the grant scope."""
        if COLLECTION_SCOPE_ALL in self.collection_scope:
            return True
        return set(collections) <= set(self.collection_scope)

    def permits_verb(self, required_action: str) -> bool:
        """Check 4: the tool's required capability is one the grant authorized.

        Independent of the precomputed allowlist (defense in depth): a session
        whose allowlist drifted from its verbs still fails closed here.
        """
        return required_action in self.granted_verbs

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


def _allowed_and_verbs(role: str, scopes: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The catalog tools an actor may hold, and the verb (action) set behind them."""
    allowed: list[str] = []
    verbs: set[str] = set()
    for spec in CATALOG:
        if can(role, Action(spec.required_action), scopes=scopes):
            allowed.append(spec.name)
            verbs.add(spec.required_action)
    if can(role, Action.proposals_write, scopes=scopes):
        verbs.add(PROPOSALS_WRITE)
    return tuple(allowed), tuple(sorted(verbs))


def derive_session(
    actor: VerifiedActor,
    *,
    session_id: str,
    now: datetime,
    ttl: timedelta = DEFAULT_SESSION_TTL,
) -> GatewaySession:
    """Turn a verified member token into the minimal session (FR-E09-1).

    The allowlist is the catalog filtered by what the actor may do — the
    identity matrix for human roles, token scopes for agents (``can`` fails
    closed on anything unknown). The write mode is ``propose_only`` when the
    actor may propose, else ``read_only``; nothing in v0.1 grants
    ``direct_write`` (FR-E09-4 propose-first default). Collection scope is the
    whole workspace and there is no agent context role (no memory-policy gate).
    """
    scopes = list(actor.scopes)
    allowed, verbs = _allowed_and_verbs(actor.role, scopes)
    proposes = PROPOSALS_WRITE in verbs
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
        collection_scope=(COLLECTION_SCOPE_ALL,),
        granted_verbs=verbs,
        agent_role=None,
        memory_policy_id=None,
        audit_policy=AUDIT_POLICY_STANDARD,
        grant_id=None,
    )


class SessionDerivationError(ValueError):
    """A grant cannot derive a session (bad agent role, empty verbs, …)."""


def derive_session_from_grant(
    actor: VerifiedActor,
    *,
    session_id: str,
    now: datetime,
    grant_id: str,
    resource: str,
    verbs: tuple[str, ...],
    expires_at: datetime,
    agent_role: str | None = None,
) -> GatewaySession:
    """Derive the full grant-scoped session (FR-E09-2).

    The grant is the source of truth: the allowlist and write mode narrow to the
    grant's ``verbs`` (a grant never widens the role — verbs were already checked
    against the §11 matrix at issuance, D-03), expiry is the grant's own, and an
    optional agent **context role** selects the memory policy applied to reads.
    Fails closed on an unknown agent role or an empty verb set.
    """
    if not verbs:
        raise SessionDerivationError("a grant-derived session needs at least one verb")
    if agent_role is not None and not is_agent_role(agent_role):
        raise SessionDerivationError(f"unknown agent context role: {agent_role!r}")

    verb_set = set(verbs)
    allowed = tuple(spec.name for spec in CATALOG if spec.required_action in verb_set)
    proposes = PROPOSALS_WRITE in verb_set
    memory_policy_id = policy_for(agent_role).policy_id if agent_role is not None else None
    return GatewaySession(
        session_id=session_id,
        member_id=actor.member_id,
        role=actor.role,
        token_id=actor.token_id,
        scopes=actor.scopes,
        allowed_tools=allowed,
        write_mode=WRITE_MODE_PROPOSE_ONLY if proposes else WRITE_MODE_READ_ONLY,
        created_at=now,
        expires_at=expires_at,
        collection_scope=_collection_scope_from_resource(resource),
        granted_verbs=tuple(sorted(verb_set)),
        agent_role=agent_role,
        memory_policy_id=memory_policy_id,
        audit_policy=AUDIT_POLICY_STANDARD,
        grant_id=grant_id,
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

    def put(self, session: GatewaySession) -> GatewaySession:
        """Register a pre-derived (grant) session under its transport key."""
        self._sessions[session.session_id] = session
        return session

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
