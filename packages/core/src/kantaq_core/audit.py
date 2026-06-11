"""Append-only audit log (MOD-07 / Epic E07, v0.0.5).

``write`` is the **only** path that creates an ``audit_event`` row (FR-E07-1):
every human write and agent action goes through it, tagged with the acting
``actor_id``. Rows carry optional ``before``/``after`` snapshots (FR-E07-3) —
use ``snapshot`` to dump a collection row into a JSON-safe dict.

Append-only is enforced **at the app layer** (NFR-E07-1): importing this module
(any ``import kantaq_core`` does it) installs guards that make a mutation of an
audit row raise ``AppendOnlyAuditError`` at three depths — the unit-of-work
path (``session.delete``, or flushing a modified row), bulk ORM
``update()``/``delete()`` statements, and an engine-level backstop that refuses
*any* compiled UPDATE/DELETE against ``audit_events``, which also covers
``bulk_update_mappings`` and statements issued on a bare connection. There is
deliberately no update or delete function here. Textual raw SQL is below the
app layer; tamper-evidence for that arrives with the hash chain (FR-E07-4,
v0.1 — DEBT-01).

Agent *reads* are aggregated (NFR-E07-2, RISK-06): the gateway records each read
on an ``AgentReadLog`` (thread-safe) and flushes one summary row per agent, so
the table grows with activity sessions, not with every query.

Timestamps are injectable (``now=``) so tests drive them with FakeClock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from sqlalchemy import Delete, Update, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapper, ORMExecuteState
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, SQLModel

from kantaq_db import AuditEvent

# Where a write came from. "app" = the human UI/API path, "cli" = kantaq CLI,
# "mcp" = an agent via the MCP gateway, "sync" = the sync engine replaying.
SOURCES: tuple[str, ...] = ("app", "cli", "mcp", "sync")

# Matches the audit_events.action column (VARCHAR(64)). Convention:
# "<entity>.<verb>" (ticket.update, member.invite, agent.read).
ACTION_MAX_LENGTH = 64

AGENT_READ_ACTION = "agent.read"


class AuditWriteError(ValueError):
    """An audit write was missing or malformed in its attribution."""


class AppendOnlyAuditError(RuntimeError):
    """An update or delete touched an audit row (NFR-E07-1: append-only)."""


def write(
    session: Session,
    *,
    actor_id: str,
    action: str,
    source: str,
    object_ref: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AuditEvent:
    """Append one audit row and flush it; the transaction stays the caller's.

    ``actor_id`` and ``source`` are required — an unattributed or misattributed
    audit row is worse than none, so there is no default to fall back on
    (FR-E07-1). ``before``/``after`` must be JSON-safe; use ``snapshot``.
    """
    if not actor_id or not actor_id.strip():
        raise AuditWriteError("audit rows must be attributed: actor_id is required")
    if not action or not action.strip():
        raise AuditWriteError("audit rows must name an action")
    if len(action) > ACTION_MAX_LENGTH:
        raise AuditWriteError(f"action exceeds {ACTION_MAX_LENGTH} chars: {action[:80]!r}")
    if source not in SOURCES:
        raise AuditWriteError(f"unknown source {source!r}; expected one of {SOURCES}")

    ts = now or datetime.now(UTC)
    row = AuditEvent(
        actor_id=actor_id,
        action=action,
        object_ref=object_ref,
        before=before,
        after=after,
        source=source,
        created_at=ts,
        updated_at=ts,
    )
    session.add(row)
    session.flush()
    return row


def snapshot(row: SQLModel) -> dict[str, Any]:
    """A JSON-safe dict of a collection row, for ``before``/``after`` (FR-E07-3)."""
    return row.model_dump(mode="json")


@dataclass
class _ReadTally:
    total: int = 0
    by_object: dict[str, int] = field(default_factory=dict)


class AgentReadLog:
    """In-memory roll-up of agent reads; one summary audit row per agent.

    The caller (the MCP gateway, Sprint 2) records every read and decides the
    flush cadence. Aggregation happens *before* the write because audit rows are
    append-only — there is no "increment a counter row" path. ``record`` and
    ``flush`` are thread-safe: the gateway serves concurrent agent calls.
    """

    def __init__(self) -> None:
        self._tallies: dict[str, _ReadTally] = {}
        self._lock = Lock()

    @property
    def pending(self) -> int:
        """Reads recorded and not yet flushed."""
        with self._lock:
            return sum(t.total for t in self._tallies.values())

    def record(self, actor_id: str, object_ref: str | None = None) -> None:
        if not actor_id or not actor_id.strip():
            raise AuditWriteError("agent reads must be attributed: actor_id is required")
        with self._lock:
            tally = self._tallies.setdefault(actor_id, _ReadTally())
            tally.total += 1
            if object_ref is not None:
                tally.by_object[object_ref] = tally.by_object.get(object_ref, 0) + 1

    def flush(self, session: Session, *, now: datetime | None = None) -> list[AuditEvent]:
        """Write one ``agent.read`` summary row per agent and reset the tallies."""
        with self._lock:
            tallies, self._tallies = self._tallies, {}
        return [
            write(
                session,
                actor_id=actor_id,
                action=AGENT_READ_ACTION,
                after={"reads": tally.total, "objects": tally.by_object},
                source="mcp",
                now=now,
            )
            for actor_id, tally in tallies.items()
        ]


def _deny_row_mutation(_mapper: Mapper[Any], _connection: Any, target: object) -> None:
    raise AppendOnlyAuditError(
        f"audit_events is append-only (NFR-E07-1); refusing to mutate {target!r}"
    )


def _deny_bulk_mutation(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    if mapper is not None and mapper.class_ is AuditEvent:
        raise AppendOnlyAuditError(
            "audit_events is append-only (NFR-E07-1); refusing bulk update/delete"
        )


def _deny_statement_mutation(
    _conn: Any, clauseelement: Any, multiparams: Any, params: Any, execution_options: Any
) -> None:
    """Engine-level backstop: no compiled UPDATE/DELETE ever reaches audit_events.

    Catches what the ORM hooks structurally can't: ``bulk_update_mappings`` (no
    mapper events, no do_orm_execute), table-targeted statements
    (``update(AuditEvent.__table__)``, where ``bind_mapper`` is None), and
    statements executed on a bare connection.
    """
    if isinstance(clauseelement, Update | Delete):
        table = getattr(clauseelement, "table", None)
        if table is not None and getattr(table, "name", None) == AuditEvent.__tablename__:
            raise AppendOnlyAuditError(
                "audit_events is append-only (NFR-E07-1); refusing UPDATE/DELETE"
            )


_guards_installed = False


def install_append_only_guards() -> None:
    """Idempotently install the app-layer append-only guards (NFR-E07-1).

    Runs at import of ``kantaq_core``; processes that bypass kantaq_core and
    write through the ORM directly are below the app layer (DEBT-01 until the
    v0.1 hash chain). Guards are registered on the Session and Engine *classes*
    so every session and engine in the process is covered, whoever created it.
    """
    global _guards_installed
    if _guards_installed:
        return
    event.listen(AuditEvent, "before_update", _deny_row_mutation)
    event.listen(AuditEvent, "before_delete", _deny_row_mutation)
    event.listen(SASession, "do_orm_execute", _deny_bulk_mutation)
    event.listen(Engine, "before_execute", _deny_statement_mutation)
    _guards_installed = True


install_append_only_guards()
