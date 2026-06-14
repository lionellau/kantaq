"""Conformance smoke (E27-T4, MOD-15 + MOD-17) — one signed event round-trips
client A → backend → client B, verified at every hop.

The v0.1 release gate (roadmap §2): every synced event is Ed25519-signed and
grant-verified. This is the minimal end-to-end proof of that invariant. Client A
signs an event under its capability grant; the canonical wire bytes round-trip
through decode; the backend verifies the signature + grant on ingest; client B
re-decodes those bytes, verifies again against the issuing device's root, and
folds the event to the expected state. Verification is asserted at all three
hops, against the same canonical codec the whole protocol shares (MOD-17).

``test_a_tampered_event_is_refused_at_every_hop`` is the failing-fixture proof:
a one-byte tamper is rejected at the origin, the backend, and the receiver — a
gate that cannot fail is worthless (MOD-30).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kantaq_protocol import (
    CapabilityGrant,
    decode,
    encode_canonical,
    generate_keypair,
    sign,
    sign_grant,
    verify,
)
from kantaq_sync_engine import (
    INVALID_SIGNATURE,
    VERIFY_OK,
    Event,
    EventRejected,
    VerifyContext,
    VerifyingBackend,
    verify_event,
)
from kantaq_sync_engine.events import fold_events
from kantaq_test_harness.backend import FakeBackend

NOW = 1_767_225_600  # a fixed instant inside the grant window
DEVICE = "dev_alice".ljust(26, "0")
MEMBER = "mbr_alice".ljust(26, "0")
GRANT = "grt_self".ljust(26, "0")
ENTITY = "tkt_conformance".ljust(26, "0")


def _grant(private_key: str) -> CapabilityGrant:
    base = CapabilityGrant(
        grant_id=GRANT,
        subject=MEMBER,
        issuer=DEVICE,
        resource="workspace/ws_a",
        verbs=("tickets.write",),
        issued_at=NOW - 60,
        expires_at=NOW + 3600,
    )
    return sign_grant(base, private_key)


def _event(private_key: str, *, sign_it: bool = True) -> Event:
    base = Event(
        event_id="evt" + "1".rjust(23, "0"),
        collection="tickets",
        entity_id=ENTITY,
        actor_id=MEMBER,
        actor_seq=1,
        policy_ref=GRANT,
        payload={"title": "ship v0.1"},
    )
    return sign(base, private_key) if sign_it else base


def _context(public_key: str, grant: CapabilityGrant) -> VerifyContext:
    return VerifyContext(roots={DEVICE: public_key}, grants={grant.grant_id: grant}, now=NOW)


def test_one_signed_event_round_trips_client_backend_client() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)

    # Hop 1 — client A (origin): the event is signed and self-verifies, and the
    # canonical wire form is a fixed point of encode∘decode (MOD-17 NFR-E03-1).
    event = _event(kp.private_key)
    assert event.sig is not None
    assert verify(event, kp.public_key)
    wire = encode_canonical(event)
    assert encode_canonical(decode(wire)) == wire

    # Hop 2 — backend ingest: the verifying backend checks signature + grant on
    # push and only then commits.
    backend = VerifyingBackend(FakeBackend(), context=lambda: _context(kp.public_key, grant))
    committed = backend.push([event])
    assert len(committed) == 1

    # Hop 3 — client B: pull (which re-verifies and drops anything unverifiable),
    # then independently re-decode the canonical bytes, verify against A's device
    # root, and fold to the expected state.
    received = backend.pull()
    assert len(received) == 1
    delivered = received[0].event
    redecoded = decode(encode_canonical(delivered))
    assert verify_event(redecoded, _context(kp.public_key, grant)).code == VERIFY_OK
    assert verify(redecoded, kp.public_key)
    state = fold_events([redecoded])
    assert state[ENTITY]["title"] == "ship v0.1"


def test_a_tampered_event_is_refused_at_every_hop() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    ctx = lambda: _context(kp.public_key, grant)  # noqa: E731 — tiny inline factory

    signed = _event(kp.private_key)
    tampered = replace(signed, payload={"title": "TAMPERED"})  # the sig is now stale

    # Origin hop: the tampered event no longer self-verifies.
    assert not verify(tampered, kp.public_key)

    # Backend ingest hop: push rejects atomically — nothing is committed.
    backend = VerifyingBackend(FakeBackend(), context=ctx)
    with pytest.raises(EventRejected) as excinfo:
        backend.push([tampered])
    assert excinfo.value.code == INVALID_SIGNATURE

    # Receiving hop: even if a tampered event reaches the raw backend out of
    # band, the verifying pull drops it rather than folding it.
    raw = FakeBackend()
    raw.push([tampered])
    assert VerifyingBackend(raw, context=ctx).pull() == []
    assert verify_event(tampered, _context(kp.public_key, grant)).code == INVALID_SIGNATURE
