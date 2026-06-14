"""kantaq core: tracker domain, identity, audit, resolver (MOD-03, MOD-06, MOD-07, MOD-21, MOD-22)

Scaffolded in Epic E01 (v0.0.5). ``identity`` (MOD-06) landed in Epic E06 and
``audit`` (MOD-07) in Epic E07; the remaining modules land in the epics that
own them. Importing this package installs the audit append-only guards.
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

__version__: str = "0.1.0"

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
