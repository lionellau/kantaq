"""Stdio transport seam for the compatibility harness (E11-T4; sequences after E09-T4).

Tier-1 drives the **official MCP SDK client** over in-process ASGI (HTTP) —
``FakeMCPClient`` / ``FakeAgent`` in ``mcp.py`` / ``compat.py``. Tier-2 reuses the
*same* client over the SDK's **stdio** transport once the gateway's stdio
entrypoint (E09-T4) lands. stdio has no request headers, so the capability-grant
binding a Tier-1 agent sends as ``mcp-grant-id`` / ``mcp-agent-role`` rides
**environment variables** to the spawned gateway process instead — that env-var
contract is the one interface to align with E09-T4 when it lands.

Until then ``stdio_transport_ready()`` is False and the Tier-2 suite
(``tests/compat/test_tier2.py``) skips — the S1–S6 structure + the matrix row are
prepped so the suite flips on the moment the transport and this seam are wired.

Imported per-test (it will pull the MCP SDK once implemented), never on the
pytest plugin path — the MOD-30 coverage rule.
"""

from __future__ import annotations

from typing import Any

# Flip to True only when BOTH the gateway stdio transport (E09-T4) and the stdio
# client below are wired. A single flag keeps Tier-2 from breaking CI in the
# window where the transport exists but the harness does not (or vice-versa):
# the suite runs exactly when the whole path is real.
_HARNESS_STDIO_WIRED = False


def stdio_transport_ready() -> bool:
    """Is the Tier-2 stdio path runnable end to end (transport + harness wired)?"""
    return _HARNESS_STDIO_WIRED


def connect_stdio(*args: Any, **kwargs: Any) -> Any:
    """Drive a real agent over the SDK stdio transport — the Tier-2 analog of a
    ``FakeAgent`` over ``FakeMCPClient``.

    Wire against E09-T4: spawn the gateway's stdio server as a child process, run
    the MCP SDK ``stdio_client`` over its stdin/stdout, and bind the capability
    grant via env vars (``mcp-grant-id`` / ``mcp-agent-role`` have no header over a
    pipe). Decode ``CallToolResult`` to the same uniform ``ToolCall`` the Tier-1
    ``FakeAgent`` returns, so the S1–S6 assertions are byte-for-byte the T1–T6
    ones with only the transport swapped.
    """
    raise NotImplementedError(
        "Tier-2 stdio harness lands with E09-T4 — implement the SDK stdio "
        "transport + env-var grant binding here (mirror FakeMCPClient/FakeAgent), "
        "then set _HARNESS_STDIO_WIRED = True."
    )
