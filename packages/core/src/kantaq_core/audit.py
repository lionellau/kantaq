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
app layer — the guards cannot *refuse* it, but the hash chain (FR-E07-4) makes
it *evident*: ``write`` links every row into a tamper-evident chain and
``verify_chain`` recomputes it, so a below-app-layer edit, removal, insertion,
or reorder of a row shows up as a named failure.

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
from sqlmodel import Session, SQLModel, col, select

from kantaq_db import AuditEvent
from kantaq_protocol import chain_hash

# Where a write came from. "app" = the human UI/API path, "cli" = kantaq CLI,
# "mcp" = an agent via the MCP gateway, "sync" = the sync engine replaying.
SOURCES: tuple[str, ...] = ("app", "cli", "mcp", "sync")

# Matches the audit_events.action column (VARCHAR(64)). Convention:
# "<entity>.<verb>" (ticket.update, member.invite, agent.read).
ACTION_MAX_LENGTH = 64

AGENT_READ_ACTION = "agent.read"

# The immutable row fields bound into each hash-chain link (FR-E07-4). The
# whole record is committed — not just the id — so a below-app-layer *content*
# edit (which leaves the id untouched) is evident, not only insertion/removal.
# Order is irrelevant: the canonical codec sorts keys. ``created_at`` is bound
# as exact integer microseconds UTC (the codec carries no floats or datetimes),
# detecting a backdated row; ``updated_at`` is excluded because an audit row
# never updates (it always equals ``created_at``), and the privacy_class
# envelope columns are excluded because they are write-time constants, not the
# "what happened" content tamper-evidence protects.
_CHAINED_FIELDS = (
    "id",
    "actor_seq",
    "created_at",
    "actor_id",
    "action",
    "object_ref",
    "source",
    "before",
    "after",
)

# Structured verification reasons (wire vocabulary, like the FR-E03-5 / grant codes).
CHAIN_OK = "ok"
CHAIN_EMPTY = "empty"  # no rows in the range — vacuously intact
CHAIN_UNCHAINED = "unchained"  # a row with no chain_hash (pre-v0.1 / DEBT-01, or nulled)
CHAIN_TAMPERED = "tampered"  # stored hash != recomputed link (edit/remove/insert/reorder)
CHAIN_TRUNCATED = "truncated"  # the range does not reach the expected tip (tail removed)

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class AuditWriteError(ValueError):
    """An audit write was missing or malformed in its attribution."""


class AppendOnlyAuditError(RuntimeError):
    """An update or delete touched an audit row (NFR-E07-1: append-only)."""


@dataclass(frozen=True)
class ChainVerification:
    """The outcome of one ``verify_chain`` pass: ``ok`` or a named failure.

    ``event_id`` is the id of the first row where the chain diverges (or the
    last row checked, for ``truncated``), so an auditor can point at the gap.
    """

    ok: bool
    reason: str
    event_id: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _epoch_micros(ts: datetime) -> int:
    """Exact integer microseconds since the Unix epoch, UTC (no float rounding).

    kantaq always writes UTC; a naive datetime (SQLite reads strip the tzinfo)
    is read as UTC, so the value is identical whether the row is in memory,
    SQLite, or Postgres — the chain link is reproducible across stores.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = ts - _UNIX_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _chain_record(row: AuditEvent) -> dict[str, Any]:
    """The canonical, in-profile content of a row that the chain commits to.

    Built from ``_CHAINED_FIELDS`` so the bound set is one source of truth;
    ``created_at`` is the only field needing a serialization (to int micros).
    """
    record: dict[str, Any] = {}
    for name in _CHAINED_FIELDS:
        value = getattr(row, name)
        record[name] = _epoch_micros(value) if name == "created_at" else value
    return record


def _current_tip(session: Session) -> str | None:
    """The ``chain_hash`` of the most recent row (max ULID), or None if empty.

    The local runtime is the only audit writer (gateway-written only, MOD-07),
    and ULIDs are monotonic, so the max-id row is always the chain's tip.
    """
    return session.exec(
        select(AuditEvent.chain_hash).order_by(col(AuditEvent.id).desc()).limit(1)
    ).first()


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
    (FR-E07-1). ``before``/``after`` must be canonically encodable (the codec's
    restricted RFC 8785 profile: no floats); ``snapshot`` produces such dicts.

    The row is linked into the hash chain (FR-E07-4) before it is flushed:
    ``chain_hash = H(previous_tip ‖ this row's content)``. Nothing is written
    if the content is outside the canonical profile — the ``SchemaViolation``
    is raised here, before ``session.add``.
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
    # Link before add: the tip query must not autoflush this row, and a
    # non-canonical payload must fail before anything is persisted.
    row.chain_hash = chain_hash(_current_tip(session), _chain_record(row))
    session.add(row)
    session.flush()
    return row


def read_range(
    session: Session,
    *,
    actor_id: str | None = None,
    action: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    """Most-recent-first audit rows, optionally filtered (E20-T3 trust surface).

    A live read straight off the append-only log — the Agents page and the
    Inbox's denied-calls tab call it on every poll, never a cache (NFR-E20-1),
    so a denial is visible the instant it is written. ``actor_id`` scopes to one
    member's trail; ``action`` narrows to one kind (``"tool.deny"`` for denied
    calls); ``source`` to one origin (``"mcp"`` for agent calls). Newest first by
    ``created_at`` with the ULID ``id`` as a stable tiebreak; ``limit`` is clamped
    by the caller (the API caps it) so a range read cannot scan the whole log.
    """
    stmt = select(AuditEvent)
    if actor_id is not None:
        stmt = stmt.where(col(AuditEvent.actor_id) == actor_id)
    if action is not None:
        stmt = stmt.where(col(AuditEvent.action) == action)
    if source is not None:
        stmt = stmt.where(col(AuditEvent.source) == source)
    stmt = stmt.order_by(col(AuditEvent.created_at).desc(), col(AuditEvent.id).desc()).limit(limit)
    return list(session.exec(stmt).all())


def mcp_actor_ids(session: Session) -> set[str]:
    """Distinct actors that have made an MCP gateway call (``source="mcp"``).

    The Agents page uses this for completeness (NFR-E20-1): a capability grant
    whose subject has *any* gateway activity is a real session and must be
    shown — even if the subject's member role isn't Agent, or its member row is
    gone. Completeness over neatness: a used grant is never hidden.
    """
    rows = session.exec(
        select(col(AuditEvent.actor_id)).where(col(AuditEvent.source) == "mcp").distinct()
    ).all()
    return set(rows)


def verify_chain(
    session: Session,
    *,
    start_id: str | None = None,
    end_id: str | None = None,
    expected_tip: str | None = None,
) -> ChainVerification:
    """Recompute the audit hash chain over a range and report the first break.

    Walks ``audit_events`` in ULID (``id``) order — which is insertion order —
    recomputing each row's link from the running predecessor hash and comparing
    it to the stored ``chain_hash``. Any below-app-layer tamper that the
    append-only guards cannot refuse is caught:

    - a **content edit** or a **reordered/forged** row → its recomputed link
      no longer matches its stored hash (``CHAIN_TAMPERED``);
    - a **removed interior** row → the *next* row's predecessor is wrong, so its
      link fails (``CHAIN_TAMPERED`` at that row);
    - a **removed tail** row → the chain stays internally consistent but no
      longer reaches ``expected_tip`` (``CHAIN_TRUNCATED``); pass the anchor you
      expect the range to end on to detect truncation;
    - a row that was never chained (pre-v0.1 / DEBT-01) → ``CHAIN_UNCHAINED``.

    ``start_id``/``end_id`` bound the range (inclusive); when ``start_id`` is
    given, the chain_hash of the row just before it seeds the walk — that
    boundary row is the trusted anchor of a "verified range". Omit ``start_id``
    to verify from genesis. ``populate_existing`` forces a fresh read so a tamper
    applied out-of-band is seen even if the session has the row cached.
    """
    previous: str | None = None
    if start_id is not None:
        previous = session.exec(
            select(AuditEvent.chain_hash)
            .where(col(AuditEvent.id) < start_id)
            .order_by(col(AuditEvent.id).desc())
            .limit(1)
        ).first()

    stmt = select(AuditEvent).order_by(col(AuditEvent.id).asc())
    if start_id is not None:
        stmt = stmt.where(col(AuditEvent.id) >= start_id)
    if end_id is not None:
        stmt = stmt.where(col(AuditEvent.id) <= end_id)
    rows = session.exec(stmt.execution_options(populate_existing=True)).all()

    last_id: str | None = None
    for row in rows:
        if row.chain_hash is None:
            return ChainVerification(False, CHAIN_UNCHAINED, row.id)
        if chain_hash(previous, _chain_record(row)) != row.chain_hash:
            return ChainVerification(False, CHAIN_TAMPERED, row.id)
        previous = row.chain_hash
        last_id = row.id

    if expected_tip is not None and previous != expected_tip:
        return ChainVerification(False, CHAIN_TRUNCATED, last_id)
    if last_id is None:
        return ChainVerification(True, CHAIN_EMPTY, None)
    return ChainVerification(True, CHAIN_OK, last_id)


def snapshot(row: SQLModel) -> dict[str, Any]:
    """A JSON-safe dict of a collection row, for ``before``/``after`` (FR-E07-3)."""
    return row.model_dump(mode="json")


@dataclass
class _ReadTally:
    total: int = 0
    byte_total: int = 0
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

    def record(
        self, actor_id: str, object_ref: str | None = None, *, payload_bytes: int = 0
    ) -> None:
        if not actor_id or not actor_id.strip():
            raise AuditWriteError("agent reads must be attributed: actor_id is required")
        if payload_bytes < 0:
            raise AuditWriteError("agent read payload_bytes must be non-negative")
        with self._lock:
            tally = self._tallies.setdefault(actor_id, _ReadTally())
            tally.total += 1
            tally.byte_total += payload_bytes
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
                after={
                    "reads": tally.total,
                    "bytes": tally.byte_total,
                    "objects": tally.by_object,
                },
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
