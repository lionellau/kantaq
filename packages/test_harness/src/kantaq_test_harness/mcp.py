"""FakeMCPClient: a real agent over MCP, without a socket (MOD-30, with E09).

The Gateway/Agent profile needs "a real agent over MCP". Rather than fake the
protocol, this drives the **official MCP SDK client** (the same code Claude
Code-class agents embed) against the gateway's ASGI app over an in-process
httpx ``ASGITransport`` — a full streamable-HTTP handshake (initialize,
session id, tools/list, tools/call) with no network, satisfying both the
hermetic rule and the "a fake honors the real contract" rule by construction:
the client half *is* the real component.

Sync facade: gateway tests (like the rest of the suite) are synchronous, so
the async SDK runs on a blocking-portal event loop. The transport, client
session, and the app lifespan (which starts the server transport's task group
and flushes aggregated reads on exit) all live in **one** long-lived portal
task — anyio cancel scopes must enter and exit in the same task — while
individual tool calls hop onto the loop per call.

Imported per-test, never on the pytest plugin path — this module pulls the
MCP SDK and httpx (the MOD-30 coverage rule).
"""

from __future__ import annotations

from contextlib import AsyncExitStack, ExitStack
from types import TracebackType
from typing import TYPE_CHECKING, Any

import anyio
import anyio.from_thread
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, InitializeResult, ListToolsResult
from starlette.applications import Starlette

if TYPE_CHECKING:
    from anyio.abc import TaskStatus

# Loopback host on purpose: the gateway's DNS-rebinding protection only
# admits loopback Host headers, and a real agent talks to 127.0.0.1 too.
DEFAULT_MCP_URL = "http://127.0.0.1/v1/mcp"


class FakeMCPClient:
    """Drive an MCP server ASGI app like a real agent. Use as a context manager.

    ``token`` rides every request as the bearer the gateway demands; pass
    ``token=None`` (or a bad one) to exercise the deny path — ``__enter__``
    raises the transport's HTTP error exactly like a real client would.
    """

    def __init__(
        self,
        app: Starlette,
        *,
        token: str | None,
        url: str = DEFAULT_MCP_URL,
        run_lifespan: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._app = app
        self._token = token
        self._url = url
        self._run_lifespan = run_lifespan
        # Extra connection headers a real agent sends to bind a capability grant
        # (``mcp-grant-id`` / ``mcp-agent-role``, E09-T3) — set on every request
        # alongside the bearer, so a grant-derived session can be exercised over
        # the real wire (the compatibility profile's Tier-1 path, E11-T2).
        self._extra_headers = dict(extra_headers or {})
        self._sync_stack = ExitStack()
        self._session: ClientSession | None = None
        self._get_session_id: Any = None
        self._close_event: anyio.Event | None = None
        self.initialize_result: InitializeResult | None = None

    # ------------------------------------------------------------- lifecycle

    async def _runner(self, *, task_status: TaskStatus[None]) -> None:
        """Owns every async context for the connection's whole lifetime."""
        self._close_event = anyio.Event()
        async with AsyncExitStack() as stack:
            if self._run_lifespan:
                await stack.enter_async_context(self._app.router.lifespan_context(self._app))
            headers = dict(self._extra_headers)
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self._app),
                    base_url="http://127.0.0.1",
                    headers=headers,
                    # The SDK's own client factory follows redirects; match it.
                    follow_redirects=True,
                )
            )
            read, write, get_session_id = await stack.enter_async_context(
                streamable_http_client(self._url, http_client=http_client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            self.initialize_result = await session.initialize()
            self._session = session
            self._get_session_id = get_session_id
            task_status.started()
            await self._close_event.wait()

    def __enter__(self) -> FakeMCPClient:
        self._portal = self._sync_stack.enter_context(anyio.from_thread.start_blocking_portal())
        try:
            self._runner_future, _ = self._portal.start_task(self._runner)
        except BaseException:
            self._sync_stack.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._close_event is not None:
                event = self._close_event
                self._portal.call(event.set)
                self._runner_future.result(timeout=30)
        finally:
            self._session = None
            self._sync_stack.close()

    # ------------------------------------------------------------------ calls

    @property
    def session_id(self) -> str | None:
        """The transport's mcp-session-id (the gateway session key)."""
        return self._get_session_id() if self._get_session_id is not None else None

    def list_tools(self) -> ListToolsResult:
        return self._portal.call(self._live_session().list_tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        session = self._live_session()

        async def _call() -> CallToolResult:
            return await session.call_tool(name, arguments or {})

        return self._portal.call(_call)

    def _live_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("FakeMCPClient must be entered as a context manager first")
        return self._session


class FakeStdioMCPClient:
    """Drive the gateway's **stdio**-configured MCP server like a real stdio agent.

    The stdio analog of :class:`FakeMCPClient` (E09-T4): instead of the HTTP ASGI
    transport, it connects the real SDK ``ClientSession`` to the server over the
    SDK's in-memory client↔server streams (``create_connected_server_and_client_session``).
    The server is the one ``kantaq mcp stdio`` runs (``build_stdio_server``), so
    the deny matrix / audit / round-trip are proven against the same wiring the
    CLI serves — without spawning a subprocess (hermetic; the real-pipe path gets
    one separate smoke). The bearer token + grant are baked into the server's
    stdio resolver (the env-var contract), not sent per call.

    Same sync facade as ``FakeMCPClient``: the async SDK runs on one long-lived
    blocking-portal task; individual calls hop onto the loop per call.
    """

    def __init__(self, server: Server[Any, Any]) -> None:
        self._server = server
        self._sync_stack = ExitStack()
        self._session: ClientSession | None = None
        self._close_event: anyio.Event | None = None
        self.initialize_result: InitializeResult | None = None

    async def _runner(self, *, task_status: TaskStatus[None]) -> None:
        self._close_event = anyio.Event()
        async with create_connected_server_and_client_session(self._server) as session:
            # The helper runs server.run + the client initialize handshake; a
            # usable session here means initialize succeeded.
            self._session = session
            task_status.started()
            await self._close_event.wait()

    def __enter__(self) -> FakeStdioMCPClient:
        self._portal = self._sync_stack.enter_context(anyio.from_thread.start_blocking_portal())
        try:
            self._runner_future, _ = self._portal.start_task(self._runner)
        except BaseException:
            self._sync_stack.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._close_event is not None:
                event = self._close_event
                self._portal.call(event.set)
                self._runner_future.result(timeout=30)
        finally:
            self._session = None
            self._sync_stack.close()

    def list_tools(self) -> ListToolsResult:
        return self._portal.call(self._live_session().list_tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        session = self._live_session()

        async def _call() -> CallToolResult:
            return await session.call_tool(name, arguments or {})

        return self._portal.call(_call)

    def _live_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("FakeStdioMCPClient must be entered as a context manager first")
        return self._session
