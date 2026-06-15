"""E24-T7 / DEBT-21 → E05-T1: ingesting device + grant trust roots must not wedge.

Before E24-T7 the trust roots (``devices``/``capability_grants``) were off the
applier's surface, so a broad ``pull(collection=None)`` that hit a device or
grant event raised ``UnknownCollectionError`` — aborting the ingest transaction
so the cursor never advanced and the replica re-pulled the poisoned batch
forever (the wedge MOD-05 line 65 flags). E24-T7 put them on the surface.

E05-T1 (MOD-26 §B2) lands the final shape: trust roots stay on the applier
surface (``SYNCABLE_MODELS``) but fold through a DEDICATED identity ingest
(``ingest_trust_root``) — they are NOT in the domain fold (``DOMAIN_MODELS``),
so E05-T2's per-field conflict engine + sticky-tombstone rules never touch
identity state. ``apply_inbox`` is the named §B2 entry point.
"""

from __future__ import annotations

from kantaq_db import CapabilityGrantRow, Device
from kantaq_sync_engine import DOMAIN_MODELS, SYNCABLE_MODELS, TRUST_ROOT_MODELS
from kantaq_sync_engine.events import Event
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, Replica


def _device_event(seq: int) -> Event:
    row = Device(
        id="dev_peer000000000000000001",
        public_key="b" * 64,
        member_id=None,
        label="peer laptop",
    )
    return Event(
        event_id=f"evt_dev_{seq:017d}",
        collection="devices",
        entity_id=row.id,
        actor_id="mbr_b00000000000000000000",
        actor_seq=seq,
        op="patch",
        payload=row.model_dump(mode="json"),
    )


def _grant_event(seq: int) -> Event:
    row = CapabilityGrantRow(
        id="grt_peer00000000000000001",
        subject="mbr_b00000000000000000000",
        issuer="dev_peer000000000000000001",
        resource=WORKSPACE_ID,
        verbs=["tickets.write"],
        issued_at=0,
        expires_at=2_000_000_000,
    )
    return Event(
        event_id=f"evt_grt_{seq:017d}",
        collection="capability_grants",
        entity_id=row.id,
        actor_id="mbr_b00000000000000000000",
        actor_seq=seq,
        op="patch",
        payload=row.model_dump(mode="json"),
    )


def test_trust_roots_are_on_the_applier_surface_via_a_dedicated_ingest() -> None:
    """They are foldable (on the surface, so the allowlist gate + export cover
    them) but routed to the identity ingest — NOT the domain fold (B2)."""
    for name in ("devices", "capability_grants"):
        assert name in SYNCABLE_MODELS  # on the surface (export + allowlist parity)
        assert name in TRUST_ROOT_MODELS  # routed to the dedicated identity ingest
        assert name not in DOMAIN_MODELS  # never the domain optimistic_db fold


def test_apply_inbox_over_device_and_grant_events_does_not_wedge(
    bob: Replica, backend: FakeBackend
) -> None:
    """A peer committed a device + grant to the shared log; bob's broad inbox
    pull ingests both through the dedicated identity ingest without raising,
    and the cursor advances (the DEBT-21 wedge stays closed)."""
    backend.push([_device_event(1), _grant_event(2)])

    result = bob.sync.apply_inbox(collection=None)  # the broad pull DEBT-21 warned about

    assert result.applied == 2
    with bob.session() as session:
        device = session.get(Device, "dev_peer000000000000000001")
        grant = session.get(CapabilityGrantRow, "grt_peer00000000000000001")
        assert device is not None and device.public_key == "b" * 64
        assert grant is not None and grant.resource == WORKSPACE_ID

    # The cursor moved past the batch — a re-pull ingests nothing new (no wedge).
    again = bob.sync.apply_inbox(collection=None)
    assert again.applied == 0
