"""Snapshot compose from the event log in NDJSON (E04-T3, FR-E04-4).

One JSON object per line (the JSON Lines convention, jsonlines.org), one line
per live entity, deterministically rendered: entities sorted by id, keys
sorted, compact separators. Two replicas that hold the same events produce
byte-identical snapshots — which is exactly how the convergence tests compare
replicas, and what the v0.2 export bundle builds on.
"""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session

from kantaq_core.tracker.events import DomainEvent, fold_entity
from kantaq_sync_engine.log import collection_rows


def fold_collection(session: Session, collection: str) -> dict[str, dict[str, Any]]:
    """Fold the log into {entity_id: state} in resolution order (D-05)."""
    rows = collection_rows(session, collection)
    domain_events = [
        DomainEvent(
            collection=row.collection,
            entity_id=row.entity_id,
            op=row.op,  # type: ignore[arg-type]  # the column stores the Op literal
            payload=dict(row.payload),
        )
        for row in rows
    ]
    state: dict[str, dict[str, Any]] = {}
    for entity_id in {row.entity_id for row in rows}:
        folded = fold_entity(entity_id, domain_events)
        if folded is not None:
            state[entity_id] = folded
    return state


def compose_snapshot(session: Session, collection: str) -> str:
    """The collection's current state as deterministic NDJSON."""
    state = fold_collection(session, collection)
    lines = [
        json.dumps(
            {"collection": collection, "entity_id": entity_id, "state": state[entity_id]},
            sort_keys=True,
            separators=(",", ":"),
        )
        for entity_id in sorted(state)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def parse_snapshot(ndjson: str) -> dict[str, dict[str, Any]]:
    """NDJSON → {entity_id: state}; the inverse of ``compose_snapshot``."""
    state: dict[str, dict[str, Any]] = {}
    for line in ndjson.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        state[record["entity_id"]] = record["state"]
    return state
