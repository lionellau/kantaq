"""The loopback MCP server (E09-T1, FR-E09-1).

Official Python MCP SDK, low-level server, streamable HTTP transport, mounted
at ``/v1/mcp`` behind bearer-token auth. Security posture (PRD §8.5 reference
implementation):

- binds ``127.0.0.1`` only — ``serve_gateway`` refuses any other host outright
  (no opt-in path exists in v0.0.5);
- random port by default (bind port 0, publish the real port to the user and
  to a 0600 discovery file beside the database — never the token, which stays
  in the keychain; ``kantaq token show`` prints it);
- a member bearer token is required on every request, even on localhost —
  identity failures are 401 + an audited denial;
- the gateway port serves agents, not browsers: any ``Origin`` header is
  rejected before the token is read (DNS-rebind/CSRF hardening, the same rule
  as the runtime API), and the SDK's transport-level DNS-rebinding protection
  validates ``Host`` as a second layer.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from kantaq_core.identity import VerifiedActor
from kantaq_mcp import __version__, security
from kantaq_mcp.gateway import Gateway, GatewayDenied, GrantSessionRequest
from kantaq_mcp.security import LOOPBACK_HOSTS, SYSTEM_PROMPT_TEMPLATE
from kantaq_mcp.session import GatewaySession
from kantaq_mcp.tools import ToolError

MCP_PATH = "/v1/mcp"
SESSION_INIT_PATH = "/v1/session/init"

# Headers an agent sends on the MCP connection to bind a capability grant
# (E09-T3). Absent → the v0.0.5 token-derived minimal session.
GRANT_HEADER = "mcp-grant-id"
AGENT_ROLE_HEADER = "mcp-agent-role"

# The loopback bind/origin rules are MOD-18's, consolidated in security.py
# (E08-T3); re-exported here for the bind helpers and back-compat.
__all__ = ["LOOPBACK_HOSTS"]

_ACTOR_SCOPE_KEY = "kantaq_actor"


class GatewayBindError(ValueError):
    """The gateway was asked to bind a non-loopback interface."""


class BearerAuthMiddleware:
    """Origin-then-token gate in front of the MCP transport.

    Pure ASGI (no response buffering): a present ``Origin`` header is rejected
    before the token is read — no browser page has any business on this port —
    then the bearer token must verify to a live member. The verified actor
    rides the ASGI scope to the tool handlers.
    """

    def __init__(self, app: ASGIApp, gateway: Gateway) -> None:
        self._app = app
        self._gateway = gateway

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - lifespan/ws passthrough
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if security.is_browser_origin(headers.get("origin")):
            response = JSONResponse({"detail": "origin not allowed"}, status_code=403)
            await response(scope, receive, send)
            return
        scheme, _, credentials = headers.get("authorization", "").partition(" ")
        token = credentials.strip() if scheme.lower() == "bearer" else ""
        actor = self._gateway.authenticate(token or None)
        if actor is None:
            detail = (
                "invalid or revoked token" if token else "bearer token required (even on localhost)"
            )
            self._gateway.audit_identity_denial(detail=detail)
            response = JSONResponse(
                {"detail": detail},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})[_ACTOR_SCOPE_KEY] = actor
        await self._app(scope, receive, send)


def _request_actor_session(
    server: Server[Any, Any], gateway: Gateway
) -> tuple[VerifiedActor, GatewaySession]:
    """The authenticated actor + gateway session for the current MCP request."""
    request = server.request_context.request
    if not isinstance(request, Request):  # pragma: no cover - HTTP transport always sets it
        raise RuntimeError("kantaq gateway requires the HTTP transport request context")
    actor: VerifiedActor = request.scope["state"][_ACTOR_SCOPE_KEY]
    session_id = request.headers.get("mcp-session-id") or "pre-initialize"
    grant_id = request.headers.get(GRANT_HEADER)
    grant_request = (
        GrantSessionRequest(
            grant_id=grant_id,
            agent_role=request.headers.get(AGENT_ROLE_HEADER) or None,
        )
        if grant_id
        else None
    )
    return actor, gateway.session_for(actor, session_id=session_id, grant_request=grant_request)


def _error_result(code: str, message: str) -> types.CallToolResult:
    payload = {"error": {"code": code, "message": message}}
    return types.CallToolResult(
        isError=True,
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
    )


def build_mcp_server(gateway: Gateway) -> Server[Any, Any]:
    """The low-level MCP server: list_tools/call_tool wired through the gateway."""
    server: Server[Any, Any] = Server(
        "kantaq-gateway",
        version=__version__,
        instructions=SYSTEM_PROMPT_TEMPLATE,
    )

    # The SDK's decorator factories are untyped (`def list_tools(self):`);
    # scoped ignores rather than losing strict mode for the module.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        _, session = _request_actor_session(server, gateway)
        return [
            types.Tool(
                name=spec.name,
                title=spec.title,
                description=spec.description,
                inputSchema=spec.input_schema,
                outputSchema=spec.output_schema,
            )
            for spec in gateway.allowed_specs(session)
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | types.CallToolResult:
        actor, session = _request_actor_session(server, gateway)
        try:
            return gateway.handle_call(actor=actor, session=session, tool_name=name, args=arguments)
        except GatewayDenied as denied:
            return _error_result(denied.reason, denied.message)
        except ToolError as exc:
            return _error_result(exc.code, exc.message)

    return server


def build_gateway_app(
    gateway: Gateway, *, on_shutdown: Callable[[], None] | None = None
) -> Starlette:
    """The ASGI app: /healthz open, /v1/mcp token-gated, reads flushed on exit.

    ``on_shutdown`` runs in the lifespan teardown — the only cleanup hook that
    still executes on SIGTERM/SIGINT: uvicorn re-raises the captured signal
    (process-fatal) as soon as its ``run()`` unwinds, so code after ``run()``
    is unreachable on the signal path.
    """
    server = build_mcp_server(gateway)
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "127.0.0.1", "localhost"],
            # No browser origin is legitimate on the agent port; the auth
            # middleware already rejected any Origin header before this layer.
            allowed_origins=[],
        ),
    )

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    async def session_init(request: Request) -> JSONResponse:
        """`POST /v1/session/init` — verify a grant, return the session descriptor.

        The explicit grant-derived session surface (FR-E09-2): an agent (or the
        snippet generator) presents its member token + ``{grant_id, agent_role?}``
        and learns the session it will get and the headers to connect with. The
        binding itself happens on the MCP transport with those headers.
        """
        if security.is_browser_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        token = credentials.strip() if scheme.lower() == "bearer" else ""
        actor = gateway.authenticate(token or None)
        if actor is None:
            detail = "invalid or revoked token" if token else "bearer token required"
            gateway.audit_identity_denial(detail=detail)
            return JSONResponse(
                {"detail": detail}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
        grant_id = body.get("grant_id") if isinstance(body, dict) else None
        if not isinstance(grant_id, str) or not grant_id:
            return JSONResponse({"detail": "grant_id (string) is required"}, status_code=400)
        agent_role = body.get("agent_role") if isinstance(body, dict) else None
        request_model = GrantSessionRequest(
            grant_id=grant_id, agent_role=agent_role if isinstance(agent_role, str) else None
        )
        try:
            descriptor = gateway.describe_grant_session(actor, request_model)
        except GatewayDenied as denied:
            return JSONResponse(
                {"error": {"code": denied.reason, "message": denied.message}}, status_code=403
            )
        descriptor["instructions"] = SYSTEM_PROMPT_TEMPLATE
        return JSONResponse(descriptor)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            try:
                yield
            finally:
                # The last aggregated agent reads must not die with the process.
                gateway.flush_reads()
                if on_shutdown is not None:
                    on_shutdown()

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route(SESSION_INIT_PATH, session_init, methods=["POST"]),
            # An exact-path ASGI route (not a Mount): the MCP endpoint is one
            # URL, and a Mount would 307-redirect "/v1/mcp" to "/v1/mcp/".
            Route(
                MCP_PATH,
                endpoint=BearerAuthMiddleware(manager.handle_request, gateway),
                methods=["POST", "GET", "DELETE"],
            ),
        ],
        lifespan=lifespan,
    )


@dataclass(frozen=True)
class GatewayBinding:
    """Where the gateway actually listens (the random port made concrete)."""

    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{MCP_PATH}"


def bind_loopback_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket ourselves so the random port is known up front.

    Refuses anything that is not loopback (FR-E09-1); ``port=0`` asks the OS
    for a free port — the PRD's "random port, published to the user only".
    """
    if not security.is_loopback_host(host):
        raise GatewayBindError(
            f"the MCP gateway binds loopback only; refusing host {host!r} "
            "(LOCAL_MCP_HOST must stay 127.0.0.1)"
        )
    return socket.create_server((host, port))


def write_discovery_file(path: Path, binding: GatewayBinding) -> None:
    """Publish the bound URL for local tooling (the E21 snippet generator).

    0600 like the keychain files. Carries no secret — the bearer token lives in
    the keychain only (``kantaq token show``).
    """
    payload = json.dumps(
        {
            "url": binding.url,
            "host": binding.host,
            "port": binding.port,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        },
        indent=2,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def serve_gateway(
    gateway: Gateway,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    discovery_path: Path | None = None,
    log_level: str = "info",
) -> None:
    """Bind, publish, and serve until interrupted (the ``kantaq mcp dev`` body)."""
    import uvicorn

    def remove_discovery() -> None:
        if discovery_path is not None:
            with contextlib.suppress(OSError):
                discovery_path.unlink()

    app = build_gateway_app(gateway, on_shutdown=remove_discovery)
    sock = bind_loopback_socket(host, port)
    binding = GatewayBinding(host=host, port=sock.getsockname()[1])
    try:
        if discovery_path is not None:
            write_discovery_file(discovery_path, binding)
        print(f"kantaq MCP gateway: {binding.url}")
        print("agents authenticate with a member bearer token (kantaq token show)")
        server = uvicorn.Server(uvicorn.Config(app, log_level=log_level))
        server.run(sockets=[sock])
    finally:
        # Unreachable on the signal path (uvicorn re-raises SIGTERM/SIGINT);
        # covers bind/startup failures and a normal serve() return. A hard
        # kill can still leave the file — consumers check its pid field.
        sock.close()
        remove_discovery()
