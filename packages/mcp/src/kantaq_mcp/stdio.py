"""The loopback MCP gateway over **stdio** (E09-T4, FR-E09-5).

The v0.3 second transport. It is a *transport*, not a new gateway: the same
``Gateway.handle_call`` eight checks, the same audit path, the same structured
deny errors, the same catalog — only the wire changes (the MCP SDK's stdio
transport over this process's stdin/stdout, instead of streamable HTTP). A
denial over stdio is byte-for-byte the decision it is over HTTP, because it is
the *same* check.

Security posture (MOD-08, PRD §8.5):

- **Loopback-only by construction.** stdio binds **no socket at all** — it is a
  pipe to the parent process that spawned this one (the agent's MCP client).
  There is no network surface, so the HTTP transport's origin/DNS-rebind/Host
  defenses have nothing to defend here; the threat model is the local parent,
  which already holds the token it passed us.
- **Bearer token over the environment.** stdio has no request headers, so the
  member token (and the optional capability grant) ride env vars the parent sets
  from the setup snippet: ``KANTAQ_MCP_TOKEN`` (required), ``KANTAQ_MCP_GRANT_ID``
  / ``KANTAQ_MCP_AGENT_ROLE`` (optional, the grant-derived session). The token is
  **re-verified on every call** (the same 3 s-cached :class:`TokenVerifier` the
  HTTP path uses), so revocation stops the session within the NFR-E06-2 budget.
- **One process, one session.** A stdio process is one agent connection — one
  gateway session, keyed by a fixed id. Expiry/kill stick to it; "re-initialize
  to continue" is "restart the subprocess" (a fresh process → a fresh session),
  the same lifecycle the HTTP transport gets from a fresh ``mcp-session-id``.

The env-var grant-binding contract here is the one the compatibility harness's
``connect_stdio`` (Tier-2, E11-T4) aligns to.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from kantaq_core.identity import VerifiedActor
from kantaq_mcp.gateway import DENY_IDENTITY, Gateway, GatewayDenied, GrantSessionRequest
from kantaq_mcp.server import build_mcp_server
from kantaq_mcp.session import GatewaySession

# Env vars the parent sets before spawning the stdio gateway (no request headers
# over a pipe). The token is required; the grant id + agent role are optional and
# select the grant-derived session, mirroring the HTTP ``mcp-grant-id`` /
# ``mcp-agent-role`` headers.
TOKEN_ENV = "KANTAQ_MCP_TOKEN"
GRANT_ENV = "KANTAQ_MCP_GRANT_ID"
AGENT_ROLE_ENV = "KANTAQ_MCP_AGENT_ROLE"

# A stdio process is exactly one session; the transport has no rotating session
# id, so we key it by a fixed sentinel (expiry/kill still apply per call).
STDIO_SESSION_ID = "stdio"


class StdioAuthError(Exception):
    """The stdio gateway could not authenticate at startup (no/invalid token)."""


@dataclass(frozen=True)
class StdioCredentials:
    """What the parent process passed us to bind a session over stdio."""

    token: str | None
    grant_id: str | None = None
    agent_role: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> StdioCredentials:
        """Read the bearer token + optional grant binding from the environment."""
        env = os.environ if environ is None else environ
        token = (env.get(TOKEN_ENV) or "").strip() or None
        grant_id = (env.get(GRANT_ENV) or "").strip() or None
        agent_role = (env.get(AGENT_ROLE_ENV) or "").strip() or None
        return cls(token=token, grant_id=grant_id, agent_role=agent_role)

    def grant_request(self) -> GrantSessionRequest | None:
        if self.grant_id is None:
            return None
        return GrantSessionRequest(grant_id=self.grant_id, agent_role=self.agent_role)


class _StdioActorSession:
    """Resolve (actor, session) for a stdio call by re-verifying the env token.

    The stdio analog of the HTTP request-context resolver: there is no per-request
    middleware, so the token is re-verified **here, every call** (cheap, 3 s
    cache) — a revoked token denies within the budget, identically to HTTP. An
    identity failure raises ``GatewayDenied`` (audited), which the server turns
    into a structured ``tools/call`` error / an empty ``tools/list``.
    """

    def __init__(self, gateway: Gateway, credentials: StdioCredentials, *, session_id: str) -> None:
        self._gateway = gateway
        self._credentials = credentials
        self._session_id = session_id

    def __call__(self, _server: Server[Any, Any]) -> tuple[VerifiedActor, GatewaySession]:
        actor = self._gateway.authenticate(self._credentials.token)
        if actor is None:
            self._gateway.audit_identity_denial(
                detail="missing or revoked token over stdio; restart the session"
            )
            raise GatewayDenied(
                DENY_IDENTITY, "missing or revoked token over stdio; restart the session"
            )
        session = self._gateway.session_for(
            actor,
            session_id=self._session_id,
            grant_request=self._credentials.grant_request(),
        )
        return actor, session


def build_stdio_server(
    gateway: Gateway,
    credentials: StdioCredentials,
    *,
    session_id: str = STDIO_SESSION_ID,
) -> Server[Any, Any]:
    """The MCP server wired for stdio: the shared handlers, the stdio resolver.

    Reused by both :func:`serve_stdio` (the real stdin/stdout loop) and the
    in-process tests (the SDK's in-memory client ↔ server helper), so the deny
    matrix / audit / round-trip are proven against the same wiring the CLI runs.
    """
    resolver = _StdioActorSession(gateway, credentials, session_id=session_id)
    return build_mcp_server(gateway, resolver)


async def _serve_stdio_async(gateway: Gateway, server: Server[Any, Any]) -> None:
    init_options = server.create_initialization_options()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        # The last aggregated agent reads must not die with the process (the stdio
        # analog of the HTTP lifespan teardown flush).
        gateway.flush_reads()


def serve_stdio(
    gateway: Gateway,
    credentials: StdioCredentials | None = None,
    *,
    session_id: str = STDIO_SESSION_ID,
) -> None:
    """Serve the gateway over this process's stdin/stdout until the pipe closes.

    Fails fast (``StdioAuthError``) if the env token is missing or invalid — the
    parent that spawned us misconfigured ``KANTAQ_MCP_TOKEN`` — after auditing the
    identity denial, rather than coming up and denying every call. Past that, the
    per-call resolver re-verifies for mid-session revocation.
    """
    creds = StdioCredentials.from_env() if credentials is None else credentials
    if gateway.authenticate(creds.token) is None:
        gateway.audit_identity_denial(detail="missing or invalid token at stdio startup")
        raise StdioAuthError(
            f"set {TOKEN_ENV} to a valid member token (`kantaq token show`) before "
            "launching the stdio gateway"
        )
    server = build_stdio_server(gateway, creds, session_id=session_id)
    anyio.run(_serve_stdio_async, gateway, server)
