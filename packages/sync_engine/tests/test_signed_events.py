"""E04-T4 — the EventLogSink signs at append, and fails closed post-cutover.

The sink is the seam where the runtime turns a domain write into a signed
protocol event: base_rev + policy_ref set first, then an Ed25519 signature over
the MOD-17 signing bytes covering them. These pin the seam directly; the live
runtime wiring is pinned in apps/local-runtime, and the verify side in E24-T5.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.tracker.events import DomainEvent
from kantaq_db import EventLog, new_ulid
from kantaq_protocol import generate_keypair, verify
from kantaq_sync_engine import (
    Event,
    EventLogSink,
    EventSigner,
    SigningRequiredError,
    insert_event,
    row_to_event,
)

ACTOR = "mbr_alice".ljust(26, "0")
OTHER = "mbr_other".ljust(26, "0")
ENTITY = "tkt_one".ljust(26, "0")
POLICY = "grnt_self".ljust(26, "0")


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _domain(entity_id: str = ENTITY) -> DomainEvent:
    return DomainEvent(
        collection="tickets", entity_id=entity_id, op="patch", payload={"title": "T"}
    )


def _only_event(engine: Engine, *, actor_id: str | None = None) -> Event:
    with Session(engine) as session:
        statement = select(EventLog)
        if actor_id is not None:
            statement = statement.where(EventLog.actor_id == actor_id)
        return row_to_event(session.exec(statement).one())


def test_sink_signs_and_sets_envelope(engine: Engine) -> None:
    """A signer makes the emitted event carry sig + policy_ref and verify."""
    kp = generate_keypair()
    signer = EventSigner(private_key=kp.private_key, policy_ref=POLICY)
    with Session(engine) as session:
        EventLogSink(session, ACTOR, signer=signer).emit(_domain())
        session.commit()

    event = _only_event(engine)
    assert event.sig is not None
    assert event.policy_ref == POLICY
    assert event.base_rev is None  # first write — no committed head yet
    assert verify(event, kp.public_key)


def test_sink_is_unsigned_without_a_signer(engine: Engine) -> None:
    """The pre-cutover / solo path is byte-for-byte what it was: no envelope."""
    with Session(engine) as session:
        EventLogSink(session, ACTOR).emit(_domain())
        session.commit()

    event = _only_event(engine)
    assert event.sig is None
    assert event.base_rev is None
    assert event.policy_ref is None
    assert not verify(event, generate_keypair().public_key)


def test_require_signed_without_signer_fails_closed(engine: Engine) -> None:
    """The local cutover invariant: no signer + require_signed never writes."""
    with Session(engine) as session, pytest.raises(SigningRequiredError):
        EventLogSink(session, ACTOR, require_signed=True)


def test_base_rev_is_the_entitys_committed_head(engine: Engine) -> None:
    """A signed write records the committed revision it was based on."""
    with Session(engine) as session:
        insert_event(
            session,
            Event(
                event_id=new_ulid(),
                collection="tickets",
                entity_id=ENTITY,
                actor_id=OTHER,
                actor_seq=1,
                op="patch",
                payload={"title": "old"},
            ),
            committed_rev=7,
        )
        session.commit()

    kp = generate_keypair()
    signer = EventSigner(private_key=kp.private_key, policy_ref=POLICY)
    with Session(engine) as session:
        EventLogSink(session, ACTOR, signer=signer).emit(_domain())
        session.commit()

    event = _only_event(engine, actor_id=ACTOR)
    assert event.base_rev == 7
    assert verify(event, kp.public_key)  # base_rev is inside the signature


def test_one_flipped_byte_breaks_the_signature(engine: Engine) -> None:
    """Tamper detection end to end through the sink (defense check)."""
    kp = generate_keypair()
    signer = EventSigner(private_key=kp.private_key, policy_ref=POLICY)
    with Session(engine) as session:
        EventLogSink(session, ACTOR, signer=signer).emit(_domain())
        session.commit()

    event = _only_event(engine)
    assert verify(event, kp.public_key)
    tampered = replace(event, payload={"title": "TAMPERED"})
    assert not verify(tampered, kp.public_key)
