"""``seed_cost_profile`` — the MOD-27 / E26-T0 calibration dataset (MOD-30).

A throwaway fixture (NOT runtime code) that inserts the as-built 6-month
4-person profile — the row counts from MOD-27's calibration table — into an
EphemeralPostgres engine (and the domain subset into a temp SQLite replica),
with **realistic per-row content** so ``pg_total_relation_size`` / ``dbstat`` are
representative. The FR-E26-1 accuracy test seeds this, reads the catalog, and
asserts ``kantaq_core.metrics`` lands within 10% (the calibration gate).

The shape mirrors the as-built schema: ORM tables via ``SQLModel.metadata`` plus
a faithful ``sync_events`` (backend-infra SQL, not an ORM model — D-07). Bulk
inserts via SQLAlchemy Core executemany keep the full-scale seed fast; ``scale``
shrinks it proportionally for quick local runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

import kantaq_db  # noqa: F401 — registers every collection on SQLModel.metadata
from kantaq_db import (
    AgentProposal,
    AuditEvent,
    Comment,
    Member,
    MemoryEntry,
    MemoryLink,
    Project,
    Ticket,
    TicketRelationship,
    Workspace,
)

# The full as-built 6-month 4-person profile (MOD-27 build notes, E26-T0).
PROFILE: dict[str, int] = {
    "workspaces": 1,
    "members": 4,
    "projects": 50,
    "tickets": 2_000,
    "comments": 10_000,
    "ticket_relationships": 3_000,
    "memory_links": 3_000,
    "agent_proposals": 980,
    "memory_entries": 3_500,
    "audit_events": 252_000,
    "sync_events": 120_000,
}

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_WS_ID = "ws" + "0" * 24

# Faithful sync_events (the 3 indexes drive its footprint) minus FK/CHECK noise.
_SYNC_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS sync_events (
    revision BIGINT GENERATED ALWAYS AS IDENTITY,
    event_id VARCHAR(26) NOT NULL,
    collection VARCHAR(32) NOT NULL,
    entity_id VARCHAR(26) NOT NULL,
    actor_id VARCHAR(26) NOT NULL,
    actor_seq INTEGER NOT NULL,
    op VARCHAR(16) NOT NULL,
    base_rev BIGINT,
    policy_ref VARCHAR,
    payload JSON NOT NULL,
    sig VARCHAR,
    workspace_id VARCHAR(26) NOT NULL,
    committed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (revision),
    UNIQUE (event_id),
    UNIQUE (actor_id, actor_seq)
)
"""


def _pad(seed: str, n: int) -> str:
    """Deterministic, **incompressible** filler of length ``n``.

    A repetitive string TOAST-compresses ~50× so the on-disk size would be an
    artifact of compression, not the row content. A hashed hex stream is
    incompressible (pglz keeps it verbatim), so the catalog reflects the content
    — a conservative, stable per-row footprint for the calibration gate.
    """
    if n <= 0:
        return ""
    import hashlib

    out = ""
    block = seed.encode()
    while len(out) < n:
        block = hashlib.sha256(block).hexdigest().encode()  # 64 incompressible hex chars
        out += block.decode()
    return out[:n]


def _id(prefix: str, i: int) -> str:
    """A unique 26-char id: prefix + a zero-padded counter (no truncation)."""
    return f"{prefix}{i:0{26 - len(prefix)}d}"


def _envelope(prefix: str, i: int) -> dict[str, Any]:
    return {
        "id": _id(prefix, i),
        "created_at": _EPOCH,
        "updated_at": _EPOCH,
        "actor_seq": i,
        "visibility": "team",
        "hosting_mode": "plain",
        "retention_policy": "standard",
    }


def _bulk_insert(engine: Engine, model: type, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    table = model.__table__  # type: ignore[attr-defined]
    with engine.begin() as conn:
        for start in range(0, len(rows), 5_000):
            conn.execute(table.insert(), rows[start : start + 5_000])


def _scaled(name: str, scale: float) -> int:
    base = PROFILE[name]
    if name in ("workspaces", "members"):  # tiny fixed cardinalities — keep as-is
        return base
    return max(1, round(base * scale))


def seed_cost_profile(
    engine: Engine, *, scale: float = 1.0, with_sync_events: bool = True
) -> dict[str, int]:
    """Seed the calibration profile into ``engine``; return the row counts seeded.

    On Postgres, seeds every table + ``sync_events``. On SQLite (the replica),
    seeds the domain subset (``sync_events`` is backend-only). ``scale`` shrinks
    the profile proportionally (1.0 = the full as-built counts).
    """
    is_pg = engine.dialect.name == "postgresql"
    SQLModel.metadata.create_all(engine)
    counts: dict[str, int] = {}

    project_ids = [_id("prj", i) for i in range(_scaled("projects", scale))]
    ticket_ids = [_id("tkt", i) for i in range(_scaled("tickets", scale))]

    _bulk_insert(
        engine,
        Workspace,
        [{**_envelope("ws", i), "name": "Acme"} for i in range(_scaled("workspaces", scale))],
    )
    counts["workspaces"] = _scaled("workspaces", scale)

    _bulk_insert(
        engine,
        Member,
        [
            {
                **_envelope("mbr", i),
                "workspace_id": _WS_ID,
                "email": f"dev{i}@example.com",
                "role": "Member",
                "status": "active",
            }
            for i in range(_scaled("members", scale))
        ],
    )
    counts["members"] = _scaled("members", scale)

    _bulk_insert(
        engine,
        Project,
        [
            {
                **_envelope("prj", i),
                "workspace_id": _WS_ID,
                "name": f"Project {i}",
                "goal": _pad("goal", 120),
                "scope": _pad("scope", 120),
                "status": "active",
            }
            for i in range(len(project_ids))
        ],
    )
    counts["projects"] = len(project_ids)

    _bulk_insert(
        engine,
        Ticket,
        [
            {
                **_envelope("tkt", i),
                "project_id": project_ids[i % len(project_ids)],
                "title": f"Ticket {i}: {_pad('t', 40)}",
                "description": _pad("desc", 1_400),
                "status": "todo",
                "priority": "medium",
                "labels": ["backend", "feature"],
                "acceptance_criteria": _pad("ac", 200),
                "lifecycle_stage": "implementation",
                "attachments": [],
            }
            for i in range(len(ticket_ids))
        ],
    )
    counts["tickets"] = len(ticket_ids)

    _bulk_insert(
        engine,
        Comment,
        [
            {
                **_envelope("cmt", i),
                "ticket_id": ticket_ids[i % len(ticket_ids)],
                "author_actor_id": "mbr" + "0" * 23,
                "body": _pad("comment", 900),
            }
            for i in range(_scaled("comments", scale))
        ],
    )
    counts["comments"] = _scaled("comments", scale)

    nt = len(ticket_ids)
    _bulk_insert(
        engine,
        TicketRelationship,
        # Cartesian (from=i//nt, to=i%nt) keeps every (from, to, type) pair unique.
        [
            {
                **_envelope("rel", i),
                "from_id": ticket_ids[i // nt],
                "to_id": ticket_ids[i % nt],
                "type": "related",
            }
            for i in range(_scaled("ticket_relationships", scale))
        ],
    )
    counts["ticket_relationships"] = _scaled("ticket_relationships", scale)

    mem_ids = [_id("mem", i) for i in range(_scaled("memory_entries", scale))]
    _bulk_insert(
        engine,
        MemoryEntry,
        [
            {
                **_envelope("mem", i),
                "title": f"Memory {i}",
                "body": _pad("memory body", 1_900),
                "type": "note",
                "source": "agent",
                "space": "project",
                "linked_entities": [],
                "provenance": {"origin": "agent"},
                "confidence": "medium",
                "review_status": "approved",
            }
            for i in range(len(mem_ids))
        ],
    )
    counts["memory_entries"] = len(mem_ids)

    _bulk_insert(
        engine,
        MemoryLink,
        # (ticket=i%nt, memory=i//nt) keeps every (ticket_id, memory_id) pair unique.
        [
            {
                **_envelope("mlk", i),
                "ticket_id": ticket_ids[i % nt],
                "memory_id": mem_ids[i // nt],
                "reason": _pad("reason", 80),
            }
            for i in range(_scaled("memory_links", scale))
        ],
    )
    counts["memory_links"] = _scaled("memory_links", scale)

    _bulk_insert(
        engine,
        AgentProposal,
        [
            {
                **_envelope("prop", i),
                "ticket_id": ticket_ids[i % len(ticket_ids)],
                "proposer_id": "agent" + "0" * 21,
                "diff": {"description": _pad("d", 600)},
                "status": "pending",
            }
            for i in range(_scaled("agent_proposals", scale))
        ],
    )
    counts["agent_proposals"] = _scaled("agent_proposals", scale)

    _bulk_insert(
        engine,
        AuditEvent,
        [
            {
                **_envelope("aud", i),
                "actor_id": "mbr" + "0" * 23,
                "action": "ticket.update",
                "object_ref": f"tickets/{ticket_ids[i % len(ticket_ids)]}",
                "before": {"status": "todo", "snapshot": _pad("b", 180)},
                "after": {"status": "doing", "snapshot": _pad("a", 180)},
                "source": "mcp",
                "chain_hash": "h" * 64,
            }
            for i in range(_scaled("audit_events", scale))
        ],
    )
    counts["audit_events"] = _scaled("audit_events", scale)

    if is_pg and with_sync_events:
        n = _scaled("sync_events", scale)
        with engine.begin() as conn:
            conn.execute(text(_SYNC_EVENTS_DDL))
            payload = {"op": "patch", "fields": {"description": _pad("p", 380)}}
            rows = [
                {
                    "event_id": _id("evt", i),
                    "collection": "tickets",
                    "entity_id": ticket_ids[i % len(ticket_ids)],
                    "actor_id": "mbr" + "0" * 23,
                    "actor_seq": i,
                    "op": "patch",
                    "base_rev": None,
                    "policy_ref": None,
                    "payload": payload,
                    "sig": None,
                    "workspace_id": _WS_ID,
                    "committed_at": _EPOCH,
                }
                for i in range(n)
            ]
            for start in range(0, len(rows), 5_000):
                conn.execute(
                    text(
                        "INSERT INTO sync_events "
                        "(event_id, collection, entity_id, actor_id, actor_seq, op, base_rev, "
                        "policy_ref, payload, sig, workspace_id, committed_at) VALUES "
                        "(:event_id, :collection, :entity_id, :actor_id, :actor_seq, :op, "
                        ":base_rev, :policy_ref, :payload, :sig, :workspace_id, :committed_at)"
                    ).bindparams(),
                    [{**r, "payload": _json(r["payload"])} for r in rows[start : start + 5_000]],
                )
        counts["sync_events"] = n

    # Refresh planner stats so pg_stat_user_tables.n_live_tup is populated.
    if is_pg:
        with engine.begin() as conn:
            conn.execute(text("ANALYZE"))
    return counts


def _json(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
