"""DEBT-45 — the member announce seam + the trust-root placeholder guard.

Hermetic halves of docs/design/member-events.md: §2a's ``ensure_member_event``
announces the runtime's own member row exactly once, and §2b's ingest guard
materialises a placeholder member for a legacy device/grant stream, healed in
place when the real announce folds over it. (The end-to-end repro against the
real ASGI sync-server lives in tests/test_member_event_sync.py.)
"""

from __future__ import annotations

from kantaq_db import Member
from kantaq_sync_engine import EventLogSink, ensure_member_event, entity_rows
from kantaq_sync_engine.events import Event
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, Replica

PEER_MEMBER = "mbr_peer00000000000000000"
PEER_DEVICE = "dev_peer000000000000000001"


def _own_member(replica: Replica, email: str = "alice@acme.dev") -> None:
    with replica.session() as session:
        session.add(
            Member(
                id=replica.actor_id,
                workspace_id=WORKSPACE_ID,
                email=email,
                role="Owner",
                status="active",
            )
        )
        session.commit()


def _peer_device_event(seq: int) -> Event:
    return Event(
        event_id=f"evt_dev_{seq:017d}",
        collection="devices",
        entity_id=PEER_DEVICE,
        actor_id=PEER_MEMBER,
        actor_seq=seq,
        op="patch",
        payload={
            "id": PEER_DEVICE,
            "public_key": "b" * 64,
            "member_id": PEER_MEMBER,
            "label": "peer laptop",
        },
    )


def _peer_announce_event(seq: int) -> Event:
    return Event(
        event_id=f"evt_mbr_{seq:017d}",
        collection="members",
        entity_id=PEER_MEMBER,
        actor_id=PEER_MEMBER,
        actor_seq=seq,
        op="patch",
        payload={
            "id": PEER_MEMBER,
            "workspace_id": WORKSPACE_ID,
            "email": "peer@acme.dev",
            "role": "Owner",
            "status": "active",
        },
    )


# ------------------------------------------------------------ §2a: announce


def test_announce_emits_once_and_is_idempotent_across_boots(alice: Replica) -> None:
    _own_member(alice)
    with alice.session() as session:
        member = session.get(Member, alice.actor_id)
        assert member is not None
        sink = EventLogSink(session, alice.actor_id)
        assert ensure_member_event(session, member, sink) is True
        assert ensure_member_event(session, member, sink) is False  # same boot
        session.commit()

    with alice.session() as session:  # the next boot
        member = session.get(Member, alice.actor_id)
        assert member is not None
        assert ensure_member_event(session, member, EventLogSink(session, alice.actor_id)) is False
        rows = entity_rows(session, "members", alice.actor_id)
        assert len(rows) == 1
        assert rows[0].payload["email"] == "alice@acme.dev"
        assert rows[0].payload["role"] == "Owner"


# ---------------------------------------------- §2b: the placeholder guard


def test_legacy_device_stream_folds_via_a_placeholder_member(
    bob: Replica, backend: FakeBackend
) -> None:
    """A pre-fix peer pushed only their device event. The ingest satisfies the
    ``devices.member_id`` FK itself instead of wedging the pull."""
    backend.push([_peer_device_event(1)])

    result = bob.sync.apply_inbox(collection=None)

    assert result.applied == 1
    with bob.session() as session:
        placeholder = session.get(Member, PEER_MEMBER)
        assert placeholder is not None
        assert placeholder.email == ""  # identity known, profile not yet distributed
        assert placeholder.workspace_id == WORKSPACE_ID


def test_the_real_announce_heals_the_placeholder_in_place(
    bob: Replica, backend: FakeBackend
) -> None:
    """The peer upgrades and reboots: their backfilled announce folds true
    fields over the placeholder (LWW fold updates the existing row)."""
    backend.push([_peer_device_event(1)])
    bob.sync.apply_inbox(collection=None)

    backend.push([_peer_announce_event(2)])
    bob.sync.apply_inbox(collection=None)

    with bob.session() as session:
        healed = session.get(Member, PEER_MEMBER)
        assert healed is not None
        assert healed.email == "peer@acme.dev"
        assert healed.role == "Owner"
