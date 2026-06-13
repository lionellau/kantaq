"""DEBT-15 — the signing-cutover config sanity check `kantaq doctor` surfaces.

Pins the two misconfigurations the E27 review flagged: a ``sign_cutover_rev``
past the committed head (would pass events through unverified) and ``sign_events``
off while signed events already exist locally (a mis-set post-cutover replica).
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_db import new_ulid
from kantaq_runtime.cutover import cutover_health
from kantaq_sync_engine import Event, insert_event

ACTOR = "mbr_a".ljust(26, "0")
ENTITY = "tkt_1".ljust(26, "0")


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _insert(
    session: Session, seq: int, *, committed_rev: int | None = None, signed: bool = False
) -> None:
    insert_event(
        session,
        Event(
            event_id=new_ulid(),
            collection="tickets",
            entity_id=ENTITY,
            actor_id=ACTOR,
            actor_seq=seq,
            payload={"title": f"v{seq}"},
            sig=("a" * 128 if signed else None),
        ),
        committed_rev=committed_rev,
    )


def test_clean_signing_on_is_ok(engine: Engine) -> None:
    with Session(engine) as session:
        health = cutover_health(session, sign_events=True, sign_cutover_rev=0)
    assert health.ok
    assert health.committed_head == 0
    assert health.warnings == ()


def test_future_cutover_rev_warns(engine: Engine) -> None:
    with Session(engine) as session:
        _insert(session, 1, committed_rev=2, signed=True)
        session.commit()
        health = cutover_health(session, sign_events=True, sign_cutover_rev=5)
    assert not health.ok
    assert health.committed_head == 2
    assert any("past the committed head" in w for w in health.warnings)


def test_signing_off_with_signed_events_warns(engine: Engine) -> None:
    with Session(engine) as session:
        _insert(session, 1, signed=True)
        session.commit()
        health = cutover_health(session, sign_events=False, sign_cutover_rev=0)
    assert not health.ok
    assert health.signed_event_count == 1
    assert any("sign_events is off" in w for w in health.warnings)


def test_clean_signing_off_with_only_unsigned_events_is_ok(engine: Engine) -> None:
    with Session(engine) as session:
        _insert(session, 1)  # unsigned (pre-cutover / solo)
        session.commit()
        health = cutover_health(session, sign_events=False, sign_cutover_rev=0)
    assert health.ok
    assert health.signed_event_count == 0
