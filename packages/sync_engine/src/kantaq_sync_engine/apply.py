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
    CapabilityGrantRow,
    Comment,
    Device,
    Member,
    MemoryEntry,
    MemoryLink,
    Project,
    Ticket,
    TicketRelationship,
    Workspace,
)
from kantaq_sync_engine.log import entity_rows

# The optimistic_db DOMAIN collections — folded last-writer-wins by commit order
# (D-05). tokens never sync (authority local, secret material); audit_events are
# each replica's own local trail (replays write their own, source="sync").
# memory_entries/memory_links (E13): only team-visibility rows ever produce
# events — local rows never enter the log at all (NFR-E13-1, MOD-19). These are
# the collections E05-T2's per-field conflict engine + sticky-tombstone rules
# run over — the trust roots below are deliberately NOT here.
DOMAIN_MODELS: dict[str, type[SQLModel]] = {
    "workspaces": Workspace,
    "projects": Project,
    "tickets": Ticket,
    "comments": Comment,
    # ticket_relationships (E12 v0.1): typed ticket edges; created via patch,
    # removed via tombstone — folds like any lww collection.
    "ticket_relationships": TicketRelationship,
    "members": Member,
    "agent_proposals": AgentProposal,
    "memory_entries": MemoryEntry,
    "memory_links": MemoryLink,
}

# The trust roots (MOD-06): devices + the capability grants issued under them.
# They sync over the wire (teammates need each other's device keys + grants,
# E24-T7), but on the inbox they fold through a DEDICATED identity ingest, never
# the domain fold above (MOD-26 §B2 — the offline-inbox half of DEBT-21). That
# separation is load-bearing: the E05-T2 conflict engine / sticky-tombstone
# rules run over DOMAIN_MODELS only, so they can never mint a conflict_record
# against an authoritative_tx grant or resurrect a revoked device. They are
# backend-authoritative, LWW by commit order.
TRUST_ROOT_MODELS: dict[str, type[SQLModel]] = {
    "devices": Device,
    "capability_grants": CapabilityGrantRow,
}

# The full applier surface: every collection a replica can fold (domain + trust
# roots), independent of WHICH fold path each routes to. This is the set the
# export, the import round-trip, and the three-way sync allowlist gate
# (tests/test_sync_allowlists.py) pin against the backend CHECK — "what a replica
# can fold."
SYNCABLE_MODELS: dict[str, type[SQLModel]] = {**DOMAIN_MODELS, **TRUST_ROOT_MODELS}


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
    """Rebuild one entity's row as the fold of its events (no commit).

    A trust-root collection routes to the dedicated identity ingest (B2); a
    domain collection uses the optimistic_db fold; anything else raises, so a
    poisoned pull fails loudly instead of silently dropping data.
    """
    if collection in TRUST_ROOT_MODELS:
        ingest_trust_root(session, collection, entity_id)
        return
    model = DOMAIN_MODELS.get(collection)
    if model is None:
        raise UnknownCollectionError(collection)
    _fold_into(session, model, collection, entity_id)


def ingest_trust_root(session: Session, collection: str, entity_id: str) -> None:
    """Fold a ``devices``/``capability_grants`` event into the identity store via
    a dedicated path (MOD-26 §B2, the offline-inbox half of DEBT-21).

    Backend-authoritative, LWW by commit order — never the domain optimistic_db
    fold, so the E05-T2 conflict engine + sticky-tombstone rules never run over
    identity state (no conflict_record against an authoritative_tx grant, no
    resurrection of a revoked device). v0.2 folds into the same Device /
    CapabilityGrantRow tables the verifier reads; keeping it a separate function
    is the seam the conflict engine needs and where a future roots-cache refresh
    would hook in.
    """
    _fold_into(session, TRUST_ROOT_MODELS[collection], collection, entity_id)


def _fold_into(session: Session, model: type[SQLModel], collection: str, entity_id: str) -> None:
    """Materialise one entity row as the fold of its (non-rejected) events."""
    domain_events = [
        DomainEvent(
            collection=row.collection,
            entity_id=row.entity_id,
            op=row.op,  # type: ignore[arg-type]  # the column stores the Op literal
            payload=dict(row.payload),
            base_rev=row.base_rev,
            committed_rev=row.committed_rev,
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
