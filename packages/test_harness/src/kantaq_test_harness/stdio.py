"""Stdio transport seam for the compatibility harness (E11-T4; sequences after E09-T4 — landed).

Tier-1 drives the **official MCP SDK client** over in-process ASGI (HTTP) —
``FakeMCPClient`` / ``FakeAgent`` in ``mcp.py`` / ``compat.py``. Tier-2 reuses the
*same* client over the SDK's **stdio** transport: the SDK's in-memory
client↔server streams against the shared ``kantaq_mcp.stdio.build_stdio_server``
(the same wiring ``kantaq mcp stdio`` runs), with the capability-grant binding
riding :class:`~kantaq_mcp.stdio.StdioCredentials` — the env-var contract
``KANTAQ_MCP_TOKEN`` / ``KANTAQ_MCP_GRANT_ID`` / ``KANTAQ_MCP_AGENT_ROLE`` (stdio
has no request headers). A denial over stdio is byte-for-byte the HTTP decision
because it is the same ``Gateway.handle_call``.

E09-T4 (the stdio transport) and this seam are both wired, so the Tier-2 suite
(``tests/compat/test_tier2.py``) **runs** — no longer gated/skipped. The real
Codex run (pinned 0.130.0) over the actual stdin/stdout pipe is the manual
release step recorded in ``docs/clients/compatibility.md``, like Tier-1.

``connect_stdio`` is imported per-test (it pulls the MCP SDK through
``FakeStdioAgent``), never on the pytest plugin path — the MOD-30 coverage rule.
``stdio_transport_ready`` stays import-light so the suite's skip-guard is cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kantaq_mcp.gateway import Gateway
    from kantaq_test_harness.compat import FakeStdioAgent

# Both the gateway stdio transport (E09-T4) and the FakeStdioMCPClient /
# FakeStdioAgent harness are wired, so the Tier-2 path is runnable end to end.
_HARNESS_STDIO_WIRED = True


def stdio_transport_ready() -> bool:
    """Is the Tier-2 stdio path runnable end to end (transport + harness wired)?"""
    return _HARNESS_STDIO_WIRED


def connect_stdio(
    gateway: Gateway,
    *,
    token: str | None,
    grant_id: str | None = None,
    agent_role: str | None = None,
    session_id: str | None = None,
) -> FakeStdioAgent:
    """A Tier-2 agent over the SDK stdio transport — the analog of ``FakeAgent``
    over ``FakeMCPClient``. The grant binds via :class:`StdioCredentials` (the
    env-var contract), not headers. ``session_id`` keys the one stdio session;
    pass distinct ids when a single test opens more than one."""
    from kantaq_mcp.stdio import STDIO_SESSION_ID
    from kantaq_test_harness.compat import FakeStdioAgent

    return FakeStdioAgent(
        gateway,
        token=token,
        grant_id=grant_id,
        agent_role=agent_role,
        session_id=session_id or STDIO_SESSION_ID,
    )
