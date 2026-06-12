"""kantaq MCP: gateway checks, sessions, tools (MOD-08, MOD-09).

The per-member, capability-scoped, loopback-only gateway every agent call
passes through (E09), and the v0.0.5 tool catalog behind it (E10):
``ticket_get`` and ``agent_action_propose``. Tool docs live in ``docs/mcp.md``.

``kantaq_mcp.server`` (the ASGI app + uvicorn entry) is imported explicitly by
its callers — it pulls the MCP SDK and Starlette, which nothing else needs.
"""

__version__: str = "0.0.5"

from kantaq_mcp.catalog import CATALOG, CATALOG_BY_NAME, ToolSpec
from kantaq_mcp.gateway import Gateway, GatewayDenied
from kantaq_mcp.security import SYSTEM_PROMPT_TEMPLATE, neutralize_markers, tag_untrusted
from kantaq_mcp.session import GatewaySession, SessionRegistry, derive_session
from kantaq_mcp.tools import PROPOSABLE_FIELDS, ToolError

__all__ = [
    "CATALOG",
    "CATALOG_BY_NAME",
    "PROPOSABLE_FIELDS",
    "SYSTEM_PROMPT_TEMPLATE",
    "Gateway",
    "GatewayDenied",
    "GatewaySession",
    "SessionRegistry",
    "ToolError",
    "ToolSpec",
    "__version__",
    "derive_session",
    "neutralize_markers",
    "tag_untrusted",
]
