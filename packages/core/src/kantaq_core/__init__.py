"""kantaq core: tracker domain, audit, resolver, recommendations (MOD-03, MOD-07, MOD-21, MOD-22)

Scaffolded in Epic E01 (v0.0.5). ``audit`` (MOD-07) landed in Epic E07; the
remaining modules land in the epics that own them.
"""

from __future__ import annotations

from kantaq_core.audit import (
    AGENT_READ_ACTION,
    SOURCES,
    AgentReadLog,
    AppendOnlyAuditError,
    AuditWriteError,
    snapshot,
    write,
)

__version__: str = "0.0.5"

__all__ = [
    "AGENT_READ_ACTION",
    "SOURCES",
    "AgentReadLog",
    "AppendOnlyAuditError",
    "AuditWriteError",
    "__version__",
    "snapshot",
    "write",
]
