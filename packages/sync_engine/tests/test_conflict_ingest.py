"""E05-T2.3: conflict_records folds through its dedicated ingest (MOD-26 §B4).

A conflict_record is authoritative_tx — it rides the sync surface but NOT the
optimistic-domain fold. refold_entity routes it to ingest_conflict_record, which
materialises the row insert-once on the deterministic id and keeps ``status``
sticky-monotonic: once a committed event resolves it, a later re-detection never
reopens it (mirrors sticky tombstones).
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from kantaq_db import ConflictRecord, Workspace
from kantaq_sync_engine import Event, insert_event, refold_entity
from kantaq_test_harness.replica import WORKSPACE_ID

_CID = "cfr00000000000000000001"  # the deterministic conflict-record id (the entity_id)
_ACTOR = "mbr_a00000000000000000000"


def _mint_payload(status: str) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "collection": "tickets",
        "entity_id": "tkt_000000000000000000001",
        "field": "status",
        "contending_revisions": [5, 7],
        "candidate_values": {"keep_a": "doing", "keep_b": "done"},
        "base_rev": 1,
        "head_rev": 5,
        "actor": _ACTOR,
        "status": status,
    }


def _event(eid: int, seq: int, payload: dict[str, object], *, actor: str = _ACTOR) -> Event:
    return Event(
        event_id=f"evt{eid:023d}",  # unique PK; actor_seq is per-actor and distinct
        collection="conflict_records",
        entity_id=_CID,
        actor_id=actor,
        actor_seq=seq,
        op="patch",
        payload=payload,
    )


def test_conflict_record_folds_and_status_is_sticky_resolved() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Workspace(id=WORKSPACE_ID, name="W"))
        session.commit()

        # Mint (open): folds into the ConflictRecord table via the dedicated seam.
        insert_event(session, _event(1, 1, _mint_payload("open")), committed_rev=10)
        refold_entity(session, "conflict_records", _CID)
        session.commit()
        row = session.get(ConflictRecord, _CID)
        assert row is not None
        assert row.status == "open"
        assert row.field == "status"
        assert row.candidate_values == {"keep_a": "doing", "keep_b": "done"}
        assert row.contending_revisions == [5, 7]
        assert row.entity_id == "tkt_000000000000000000001"  # the contended ticket

        # Resolution: status flips to resolved.
        insert_event(
            session,
            _event(
                2, 2, {"status": "resolved", "resolved_by": _ACTOR, "resolved_choice": "keep-A"}
            ),
            committed_rev=12,
        )
        refold_entity(session, "conflict_records", _CID)
        session.commit()
        session.refresh(row)
        assert row.status == "resolved"
        assert row.resolved_choice == "keep-A"

        # Sticky: a later re-detection (another replica re-mints "open") does NOT
        # reopen the resolved record.
        insert_event(
            session,
            _event(3, 1, _mint_payload("open"), actor="mbr_b00000000000000000000"),
            committed_rev=15,
        )
        refold_entity(session, "conflict_records", _CID)
        session.commit()
        session.refresh(row)
        assert row.status == "resolved"  # sticky-monotonic — never reopened
