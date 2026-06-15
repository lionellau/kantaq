"""E05-T2.4: a same-field concurrent edit mints a conflict_record on commit.

The atomic RPC (FakeBackend.commit_events here) detects the per-field collision
on its gapless prefix and returns the rich ``conflicts[]``; the committing client
mints a ``conflict_record`` from it (deterministic id, hashed client-side) and
folds it via the dedicated ingest. No silent overwrite — the loser's value is
preserved in the record for human resolution.

Conflict mode requires the signed/``base_rev`` path (MOD-26 §B6), so the events
here carry an explicit ``base_rev`` (the log is crafted directly rather than
through the unsigned harness sink, whose ``None`` base_rev would treat every
edit as genesis).
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from kantaq_db import ConflictRecord, Workspace
from kantaq_sync_engine import Event, SyncEngine, insert_event
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID

TID = "tkt_000000000000000000001"
ME = "mbr_me0000000000000000000"
PEER = "mbr_peer00000000000000000"


def _ev(
    event_id: str, actor: str, seq: int, payload: dict[str, object], *, base_rev: int | None
) -> Event:
    return Event(
        event_id=event_id,
        collection="tickets",
        entity_id=TID,
        actor_id=actor,
        actor_seq=seq,
        op="patch",
        base_rev=base_rev,
        payload=payload,
    )


def test_flush_mints_a_conflict_record_on_a_same_field_conflict() -> None:
    backend = FakeBackend()
    # A peer committed the create (status=todo) then a status=doing edit (rev 1, 2).
    backend.commit_events(
        [_ev("evt_create00000000000001", PEER, 1, {"status": "todo", "title": "T"}, base_rev=None)]
    )
    backend.commit_events(
        [_ev("evt_doing000000000000001", PEER, 2, {"status": "doing"}, base_rev=1)]
    )

    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        session.add(Workspace(id=WORKSPACE_ID, name="W"))
        # Our local pending edit: status=done, based on rev 1 — we never saw the
        # peer's rev-2 doing.
        insert_event(
            session, _ev("evt_done0000000000000001", ME, 1, {"status": "done"}, base_rev=1)
        )
        session.commit()

    engine = SyncEngine(db, backend, actor_id=ME, workspace_id=WORKSPACE_ID)
    flush = engine.flush_outbox()

    assert flush.committed == 1  # our done committed (LWW by order)
    assert flush.minted == 1  # and a conflict_record minted for the contended status
    with Session(db) as session:
        records = session.exec(select(ConflictRecord)).all()
        assert len(records) == 1
        rec = records[0]
        assert rec.collection == "tickets"
        assert rec.entity_id == TID
        assert rec.field == "status"
        assert rec.candidate_values == {"keep_a": "doing", "keep_b": "done"}
        assert rec.head_rev == 2
        assert rec.status == "open"
        assert sorted(rec.contending_revisions) == rec.contending_revisions  # stored sorted

    # Idempotent: nothing pending, insert-once id — a re-flush mints nothing.
    again = engine.flush_outbox()
    assert again.minted == 0
    with Session(db) as session:
        assert len(session.exec(select(ConflictRecord)).all()) == 1
