"""Fold events into local replica rows (E04-T2's apply half).

The rule that makes last-writer-wins converge (D-05): a collection table is
the **fold of its event log in resolution order** (commit order, local pending
last). Ingesting a remote event therefore never patches a row directly — it
re-folds the touched entity from the full log. That handles the hard case
where a replica's own *later-committed* write was applied optimistically
before an *earlier-committed* remote write arrives: the fold puts them back in
commit order, so both replicas end on the same value.

Folding reuses ``kantaq_core.tracker.fold_entity`` — the exact fold the
MOD-03 property test pins against the service's emit stream. One fold, one
truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlmodel import Session, SQLModel

from kantaq_core.tracker.events import DomainEvent, fold_entity
from kantaq_db import (
    AgentProposal,
    Comment,
    Member,
    MemoryEntry,
    MemoryLink,
    Project,
    Ticket,
    Workspace,
)
from kantaq_sync_engine.log import entity_rows

# The syncable collections this engine can fold into rows (architecture §6).
# tokens never sync (authority local, secret material); audit_events are each
# replica's own local trail (replays write their own, source="sync").
# memory_entries/memory_links (E13): only team-visibility rows ever produce
# events — local rows never enter the log at all (NFR-E13-1, MOD-19).
SYNCABLE_MODELS: dict[str, type[SQLModel]] = {
    "workspaces": Workspace,
    "projects": Project,
    "tickets": Ticket,
    "comments": Comment,
    "members": Member,
    "agent_proposals": AgentProposal,
    "memory_entries": MemoryEntry,
    "memory_links": MemoryLink,
}


class UnknownCollectionError(Exception):
    def __init__(self, collection: str) -> None:
        super().__init__(f"cannot apply events for unknown or unsyncable collection {collection!r}")
        self.collection = collection


def _coerce_value(model: type[SQLModel], fieldname: str, value: Any) -> Any:
    """Coerce a JSON payload value back to the column type (datetimes only —
    everything else in the v0.0.5 collections is JSON-native)."""
    fieldinfo = model.model_fields.get(fieldname)
    if fieldinfo is None or value is None:
        return value
    annotation = str(fieldinfo.annotation)
    if "datetime" in annotation and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    return value


def folded_fields(collection: str, state: dict[str, Any]) -> dict[str, Any]:
    """The folded state restricted to real columns, with types restored."""
    model = SYNCABLE_MODELS.get(collection)
    if model is None:
        raise UnknownCollectionError(collection)
    return {
        fieldname: _coerce_value(model, fieldname, value)
        for fieldname, value in state.items()
        if fieldname in model.model_fields
    }


def refold_entity(session: Session, collection: str, entity_id: str) -> None:
    """Rebuild one entity's row as the fold of its events (no commit)."""
    model = SYNCABLE_MODELS.get(collection)
    if model is None:
        raise UnknownCollectionError(collection)

    domain_events = [
        DomainEvent(
            collection=row.collection,
            entity_id=row.entity_id,
            op=row.op,  # type: ignore[arg-type]  # the column stores the Op literal
            payload=dict(row.payload),
        )
        for row in entity_rows(session, collection, entity_id)
    ]
    state = fold_entity(entity_id, domain_events)
    existing = session.get(model, entity_id)

    if state is None:
        if existing is not None:
            session.delete(existing)
        return

    fields = folded_fields(collection, state)
    if existing is None:
        session.add(model(**fields))
    else:
        for fieldname, value in fields.items():
            setattr(existing, fieldname, value)
        session.add(existing)
    session.flush()
