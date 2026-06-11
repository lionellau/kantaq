"""Audit-capture helper (MOD-30), for MOD-07's Identity/Domain profile.

``AuditCapture`` reads the ``audit_events`` table through the same session (or
connection) the test writes with, so it sees uncommitted rows inside the test's
transaction. It speaks raw SQL on purpose: the harness stays a leaf dependency
(see ``db.py``) and does not import kantaq's ORM models.

    capture = AuditCapture(session)
    core_audit.write(session, actor_id="mbr_1", action="ticket.update")
    assert capture.actions() == ["ticket.update"]
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

_JSON_COLUMNS = ("before", "after")


class AuditCapture:
    """List and filter the audit rows a test produced, in insertion order."""

    def __init__(self, executor: Session | Connection) -> None:
        self._executor = executor

    def rows(self) -> list[dict[str, Any]]:
        """Every audit row as a plain dict, ordered by (created_at, id)."""
        result = self._executor.execute(text("SELECT * FROM audit_events ORDER BY created_at, id"))
        return [_decode(dict(m)) for m in result.mappings()]

    def count(self) -> int:
        return len(self.rows())

    def actions(self) -> list[str]:
        return [str(row["action"]) for row in self.rows()]

    def by_actor(self, actor_id: str) -> list[dict[str, Any]]:
        return [row for row in self.rows() if row["actor_id"] == actor_id]


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON columns, which raw SQL returns as serialized strings."""
    for key in _JSON_COLUMNS:
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row
