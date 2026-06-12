"""Protocol events and the backend port (MOD-04, architecture §6 / PRD §13.9).

``Event`` is the wire object: what the local log stores, what push submits,
what pull receives. Field-for-field it matches the MOD-30 harness model, so
``FakeBackend`` (the contract the sync engine tests target) accepts production
events without adapters. Ed25519 ``sig`` and ``base_rev`` semantics arrive in
v0.1 with MOD-17; the fields ride along from day one so nothing reshapes.

``BackendPort`` is the v0.0.5 cut of the §13.9 adapter contract — the three
calls online sync needs (submit, stream-since-cursor, snapshot). MOD-05
(Supabase) and MOD-28 (self-hosted) implement it; MOD-30's FakeBackend
satisfies it structurally.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Op = Literal["patch", "append", "tombstone"]


@dataclass(frozen=True)
class Event:
    """One committed-or-pending protocol event."""

    event_id: str
    collection: str
    entity_id: str
    actor_id: str
    actor_seq: int
    op: Op = "patch"
    base_rev: int | None = None
    policy_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sig: str | None = None


@dataclass(frozen=True)
class CommittedEvent:
    """An event with the backend's assigned commit order (D-05)."""

    revision: int
    event: Event


class BackendPort(Protocol):
    """What the sync engine needs from a backend (implemented by MOD-05/28)."""

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Commit new events in submission order; drop (actor_id, actor_seq)
        duplicates silently so a retry can never double-commit."""
        ...

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        """Committed events with revision > ``since``, in commit order."""
        ...

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """The backend's fold of a collection (LWW by commit order)."""
        ...


def fold_events(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    """Fold an ordered event stream into per-entity state (D-05 LWW).

    The backend-side fold shape: ``patch`` overwrites fields last-writer-wins,
    ``append`` accumulates under ``_appended``, ``tombstone`` removes the
    entity. One fold, one truth — MOD-30's FakeBackend and the real backend
    adapters (MOD-05/28) all fold with this function, so the contract tests
    and production cannot drift apart.
    """
    state: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.op == "tombstone":
            state.pop(event.entity_id, None)
            continue
        current = state.setdefault(event.entity_id, {})
        if event.op == "append":
            current.setdefault("_appended", []).append(event.payload)
        else:  # patch: last writer wins on scalar fields
            current.update(event.payload)
    return state
