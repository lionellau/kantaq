"""AuditCapture stays a leaf: raw SQL over audit_events, no ORM import."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from kantaq_test_harness import AuditCapture

_DDL = """
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_ref TEXT,
    before TEXT,
    after TEXT
)
"""

_INSERT = text(
    "INSERT INTO audit_events (id, created_at, actor_id, action, object_ref, before, after) "
    "VALUES (:id, :created_at, :actor_id, :action, :object_ref, :before, :after)"
)


@pytest.fixture
def conn(temp_sqlite: Engine) -> Iterator[Connection]:
    with temp_sqlite.connect() as conn:
        conn.execute(text(_DDL))
        yield conn


def _insert(conn: Connection, n: int, actor_id: str, after: str | None = None) -> None:
    conn.execute(
        _INSERT,
        {
            "id": f"aud_{n:03d}",
            "created_at": f"2026-01-01 00:00:{n:02d}",
            "actor_id": actor_id,
            "action": "ticket.update",
            "object_ref": f"tkt_{n}",
            "before": None,
            "after": after,
        },
    )


def test_rows_come_back_in_insertion_order(conn: Connection) -> None:
    for n in (2, 0, 1):
        _insert(conn, n, "mbr_alice")
    capture = AuditCapture(conn)

    assert capture.count() == 3
    assert [row["id"] for row in capture.rows()] == ["aud_000", "aud_001", "aud_002"]
    assert capture.actions() == ["ticket.update"] * 3


def test_filters_by_actor_and_decodes_json(conn: Connection) -> None:
    _insert(conn, 0, "mbr_alice", after='{"status": "done"}')
    _insert(conn, 1, "agent_bot")
    capture = AuditCapture(conn)

    alice = capture.by_actor("mbr_alice")
    assert len(alice) == 1
    assert alice[0]["after"] == {"status": "done"}
    assert capture.by_actor("nobody") == []
