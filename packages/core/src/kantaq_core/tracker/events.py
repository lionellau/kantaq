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


@dataclass(frozen=True)
class DomainEvent:
    """One tracker mutation, as the protocol will sync it."""

    collection: str
    entity_id: str
    op: Op
    payload: dict[str, Any]


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

    Returns ``None`` for an entity that never existed or was tombstoned. The
    stream order is the resolution order (D-05: the backend's commit order);
    this fold takes whatever order it is given.
    """
    state: dict[str, Any] | None = None
    for event in events:
        if event.entity_id != entity_id:
            continue
        if event.op == "tombstone":
            state = None
            continue
        if state is None:
            state = {}
        state.update(event.payload)
    return state
