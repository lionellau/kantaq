"""FakeBackend — an in-memory model of the shared sync backend.

It does the backend's minimum (architecture §2): validate, store, assign a
commit order. v0.0.5 semantics: dedup by ``(actor_id, actor_seq)`` so re-push is
idempotent (NFR-E04-2), and fold by commit order = last-writer-wins (D-05). This
is the contract the real sync engine (E04) and backend (E24) tests target.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from kantaq_test_harness.models import Event


@dataclass(frozen=True)
class CommittedEvent:
    revision: int
    event: Event


class FakeBackend:
    def __init__(self) -> None:
        self._log: list[CommittedEvent] = []
        self._seen: set[tuple[str, int]] = set()
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Append new events, assigning a monotonic revision. Duplicates (same
        actor_id+actor_seq) are silently dropped so retries cannot duplicate."""
        committed: list[CommittedEvent] = []
        for event in events:
            key = (event.actor_id, event.actor_seq)
            if key in self._seen:
                continue
            self._seen.add(key)
            self._revision += 1
            entry = CommittedEvent(revision=self._revision, event=event)
            self._log.append(entry)
            committed.append(entry)
        return committed

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        return [
            entry
            for entry in self._log
            if entry.revision > since
            and (collection is None or entry.event.collection == collection)
        ]

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """Fold the log into current entity state (LWW by commit order)."""
        state: dict[str, dict[str, Any]] = {}
        for entry in self._log:
            event = entry.event
            if event.collection != collection:
                continue
            if event.op == "tombstone":
                state.pop(event.entity_id, None)
                continue
            current = state.setdefault(event.entity_id, {})
            if event.op == "append":
                current.setdefault("_appended", []).append(event.payload)
            else:  # patch: last writer wins on scalar fields
                current.update(event.payload)
        return state

    def __len__(self) -> int:
        return len(self._log)
