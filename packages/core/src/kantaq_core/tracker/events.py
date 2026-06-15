"""Domain events — the MOD-03 → MOD-04 seam (architecture §6).

Every tracker mutation is expressed as a ``DomainEvent`` (the collection, the
entity, a ``patch``/``append``/``tombstone`` op, and a JSON-safe payload of the
fields it sets) and handed to an ``EventSink`` *in the same transaction* as the
optimistic local write. The sync engine (MOD-04 / E04) supplies the real sink:
it persists the event into the append-only log and assigns the protocol
envelope (``actor_seq``, ``event_id``, …). Until a sink is wired in, mutations
still apply locally — solo mode with no log is a valid v0.0.5 configuration,
and tests use ``RecordingSink`` to assert what would sync.

Op semantics (matches the FakeBackend fold in MOD-30 and D-05 LWW):

- ``patch`` — create or update: payload fields overwrite the entity's.
- ``append`` — insert-only collections (comments): the payload creates the row
  exactly once and is never patched afterwards (merge_policy ``append_only``).
- ``tombstone`` — the entity is gone (no tracker mutation emits one in v0.0.5;
  the op exists so folds handle the full protocol vocabulary).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Op = Literal["patch", "append", "tombstone"]

# A patch carrying this sentinel is an explicit human revive: it re-creates a
# tombstoned entity with full state even if it did not see the delete (MOD-26
# §B5). It is a control flag stripped from the folded payload — never a real
# column. Any *non-revive* write whose ``base_rev`` predates a committed
# tombstone stays deleted (it routes to an edit-vs-delete conflict at the merge).
REVIVE_FIELD = "__revive__"


@dataclass(frozen=True)
class DomainEvent:
    """One tracker mutation, as the protocol will sync it.

    ``base_rev``/``committed_rev`` are optional sync metadata (E05-T2): the fold
    needs them for the sticky-tombstone rule (§B5). They default to ``None`` so
    the MOD-03 emit stream and solo-mode mutations still construct a DomainEvent
    from just the four protocol fields, unchanged.
    """

    collection: str
    entity_id: str
    op: Op
    payload: dict[str, Any]
    base_rev: int | None = None
    committed_rev: int | None = None


class EventSink(Protocol):
    """Where emitted events go. MOD-04's event log implements this."""

    def emit(self, event: DomainEvent) -> None: ...


@dataclass
class RecordingSink:
    """An in-memory sink: tests fold ``events`` to verify the emit stream."""

    events: list[DomainEvent] = field(default_factory=list)

    def emit(self, event: DomainEvent) -> None:
        self.events.append(event)


def fold_entity(entity_id: str, events: Iterable[DomainEvent]) -> dict[str, Any] | None:
    """Fold an ordered event stream into one entity's current state.

    Returns ``None`` for an entity that never existed or is tombstoned. The
    stream order is the resolution order (D-05: the backend's commit order,
    local pending last).

    Sticky tombstones (MOD-26 §B5): once a *committed* tombstone is seen, a later
    patch revives the entity **only if** it saw the delete (``base_rev`` >= the
    tombstone's committed revision) or it is an explicit ``__revive__``. A stale
    patch that predates the committed tombstone does **not** resurrect it — that
    is the edit-vs-delete case the conflict engine records; the row stays deleted.
    A pending (not-yet-committed) tombstone still deletes locally but is not
    sticky, so an actor may revive their own optimistic delete.
    """
    state: dict[str, Any] | None = None
    tombstone_rev: int | None = None  # committed_rev of the last committed tombstone
    for event in events:
        if event.entity_id != entity_id:
            continue
        if event.op == "tombstone":
            state = None
            if event.committed_rev is not None:
                tombstone_rev = event.committed_rev
            continue
        if tombstone_rev is not None:
            saw_delete = event.base_rev is not None and event.base_rev >= tombstone_rev
            if not (saw_delete or event.payload.get(REVIVE_FIELD)):
                continue  # sticky: a stale edit never resurrects a committed delete
            tombstone_rev = None  # a legitimate revive clears the sticky state
        if state is None:
            state = {}
        state.update({k: v for k, v in event.payload.items() if k != REVIVE_FIELD})
    return state
