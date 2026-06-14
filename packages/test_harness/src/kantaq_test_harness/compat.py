"""FakeAgent: the scripted Tier-1 compatibility client (MOD-24/MOD-30, E11-T2).

The Compatibility profile needs a *scripted client* that drives the eight
Tier-1 acceptance tests (PRD §20.4, the sprint's T1–T8) the way a real Tier-1
agent (Claude Code, Cursor) does. Rather than fake the protocol or the client,
``FakeAgent`` wraps :class:`~kantaq_test_harness.mcp.FakeMCPClient` — the
official MCP SDK client over in-process ASGI — and connects with exactly the
headers a real agent sends: a member bearer token plus the capability-grant
headers (``mcp-grant-id`` / ``mcp-agent-role``, E09-T3). So the client half
*is* the real component (the same SDK Claude Code-class agents embed), and the
"a fake honors the real contract" rule holds by construction.

It decodes the SDK's ``CallToolResult`` into a uniform :class:`ToolCall`
(``ok`` / deny-or-error ``code`` / ``message`` / structured ``data``) so the
eight acceptance tests — and the out-of-CI ``scripts/compat_check.py``
real-client runner — read the same way.

Imported per-test, never on the pytest plugin path — it pulls the MCP SDK and
httpx through FakeMCPClient (the MOD-30 coverage rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Any

from mcp.types import CallToolResult
from starlette.applications import Starlette

from kantaq_test_harness.mcp import DEFAULT_MCP_URL, FakeMCPClient

# The headers a Tier-1 agent sends to bind a capability grant (kantaq_mcp.server).
GRANT_HEADER = "mcp-grant-id"
AGENT_ROLE_HEADER = "mcp-agent-role"

# The provenance fence the read tools wrap human-authored text in (PRD §15.1 /
# MOD-18); ``tag_untrusted`` emits ``<untrusted source="…">…</untrusted>``.
UNTRUSTED_OPEN = "<untrusted"
UNTRUSTED_CLOSE = "</untrusted>"


@dataclass(frozen=True)
class ToolCall:
    """A decoded MCP tool result: a success payload or a structured denial/error.

    ``ok`` is False both for a gateway denial (``code`` is one of the eight-check
    deny reasons — ``tool_allowlist``, ``verb_match``, ``write_mode``,
    ``memory_policy``, ``expiry`` …) and for a domain ``ToolError`` (``code`` is
    ``not_found`` / ``validation`` …); the two are distinguished by the code, not
    the shape, exactly as a real client sees them.
    """

    ok: bool
    code: str | None
    message: str | None
    data: dict[str, Any]

    def require(self) -> dict[str, Any]:
        """The success payload, or raise — for a Tier-1 step that must succeed."""
        if not self.ok:
            raise AssertionError(f"tool call failed: {self.code}: {self.message}")
        return self.data


def is_untrusted_wrapped(value: str) -> bool:
    """True when a returned human string is fenced as untrusted content (T6/C6)."""
    return value.startswith(UNTRUSTED_OPEN) and value.rstrip().endswith(UNTRUSTED_CLOSE)


def _decode(result: CallToolResult) -> ToolCall:
    structured = result.structuredContent if isinstance(result.structuredContent, dict) else {}
    if result.isError:
        error = structured.get("error", {})
        if not isinstance(error, dict):
            error = {}
        code = error.get("code")
        message = error.get("message")
        return ToolCall(
            ok=False,
            code=code if isinstance(code, str) else None,
            message=message if isinstance(message, str) else None,
            data=structured,
        )
    return ToolCall(ok=True, code=None, message=None, data=structured)


class FakeAgent:
    """A scripted Tier-1 MCP agent over in-process ASGI. Use as a context manager.

    ``grant_id`` / ``agent_role`` ride every request as the grant-binding headers
    a real agent sends; omit them for the minimal token-derived session
    (``kantaq mcp dev``). A bad or missing token makes ``__enter__`` raise the
    transport's HTTP error exactly like a real client (the T5 rotation / auth
    path). A fresh agent is one fresh transport session — re-binding a grant or a
    rotated token means a new ``FakeAgent`` over a fresh app, as a real client
    re-initializes.
    """

    def __init__(
        self,
        app: Starlette,
        *,
        token: str | None,
        grant_id: str | None = None,
        agent_role: str | None = None,
        url: str = DEFAULT_MCP_URL,
    ) -> None:
        headers: dict[str, str] = {}
        if grant_id is not None:
            headers[GRANT_HEADER] = grant_id
        if agent_role is not None:
            headers[AGENT_ROLE_HEADER] = agent_role
        self._client = FakeMCPClient(app, token=token, url=url, extra_headers=headers)

    def __enter__(self) -> FakeAgent:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc, tb)

    @property
    def session_id(self) -> str | None:
        """The transport's mcp-session-id (the gateway session key)."""
        return self._client.session_id

    def tool_names(self) -> set[str]:
        """The tools this session's allowlist exposes (tools/list over the wire)."""
        return {tool.name for tool in self._client.list_tools().tools}

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
        """Call one tool and decode the structured result."""
        return _decode(self._client.call_tool(name, arguments or {}))
