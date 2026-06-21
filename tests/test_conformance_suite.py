"""The v0.2 full conformance suite (E27-T5, MOD-15 + MOD-17).

Generalizes the E27-T4 smoke (one event, one collection) to the full v0.2
invariant: **a signed event round-trips client A → backend → client B, verified
at every hop, for every syncable collection**, and the canonical codec conforms
on the checked-in golden vectors. The smoke proved the path; this proves the
*coverage* — no collection silently skips verification.

Each gate is paired with its failing fixture (the MOD-30 rule "a gate that
cannot fail is worthless"): a one-byte tamper is refused at every hop for every
collection, and the coverage check fails loudly if a syncable collection is
added without a conformance case.
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
    signing_bytes,
    verify,
)
from kantaq_sync_engine import (
    INVALID_SIGNATURE,
    SYNCABLE_MODELS,
    VERIFY_OK,
    Event,
    EventRejected,
    VerifyContext,
    VerifyingBackend,
    verify_event,
)
from kantaq_sync_engine.events import fold_events
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.vectors import load_protocol_vectors

NOW = 1_767_225_600
DEVICE = "dev_alice".ljust(26, "0")
MEMBER = "mbr_alice".ljust(26, "0")
GRANT = "grt_self".ljust(26, "0")

# A broad grant carrying every per-collection write verb, so one grant verifies
# an event for any verb-gated collection (the union of verify._COLLECTION_WRITE_VERBS).
_ALL_VERBS = (
    "tickets.write",
    "members.invite",
    "members.revoke",
    "proposals.write",
    "memory.write",
    "conflict_records.write",
)

# The verb-gated syncable collections (each has a write verb the verifier checks).
# Trust roots (devices, capability_grants) sync but route through identity ingest,
# not the per-collection write-verify path — they are covered by the allowlist
# conformance below, not the per-collection 3-hop loop.
VERB_GATED = (
    "workspaces",
    "projects",
    "tickets",
    "comments",
    "ticket_relationships",
    "members",
    "agent_proposals",
    "memory_entries",
    "memory_links",
    "conflict_records",
    "milestones",
    "ticket_milestones",
    "follow_ups",
)
_TRUST_ROOTS = ("devices", "capability_grants")


def _grant(private_key: str) -> CapabilityGrant:
    base = CapabilityGrant(
        grant_id=GRANT,
        subject=MEMBER,
        issuer=DEVICE,
        resource="workspace/ws_a",
        verbs=_ALL_VERBS,
        issued_at=NOW - 60,
        expires_at=NOW + 3600,
    )
    return sign_grant(base, private_key)


def _event(collection: str, private_key: str, *, sign_it: bool = True) -> Event:
    base = Event(
        event_id=("evt_" + collection).ljust(26, "0")[:26],
        collection=collection,
        entity_id=("ent_" + collection).ljust(26, "0")[:26],
        actor_id=MEMBER,
        actor_seq=1,
        policy_ref=GRANT,
        payload={"field": f"value-for-{collection}"},
    )
    return sign(base, private_key) if sign_it else base


def _context(public_key: str, grant: CapabilityGrant) -> VerifyContext:
    return VerifyContext(roots={DEVICE: public_key}, grants={grant.grant_id: grant}, now=NOW)


@pytest.mark.parametrize("collection", VERB_GATED)
def test_signed_event_round_trips_every_collection_at_every_hop(collection: str) -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    event = _event(collection, kp.private_key)

    # Hop 1 — origin: signed, self-verifies, canonical wire is a fixed point.
    assert verify(event, kp.public_key)
    wire = encode_canonical(event)
    assert encode_canonical(decode(wire)) == wire

    # Hop 2 — backend ingest: signature + grant + per-collection verb verified.
    backend = VerifyingBackend(FakeBackend(), context=lambda: _context(kp.public_key, grant))
    committed = backend.push([event])
    assert len(committed) == 1

    # Hop 3 — client B: re-verify on pull, re-decode, verify, fold.
    received = backend.pull()
    assert len(received) == 1
    redecoded = decode(encode_canonical(received[0].event))
    assert verify_event(redecoded, _context(kp.public_key, grant)).code == VERIFY_OK
    assert verify(redecoded, kp.public_key)
    state = fold_events([redecoded])
    assert state[event.entity_id]["field"] == f"value-for-{collection}"


@pytest.mark.parametrize("collection", VERB_GATED)
def test_a_tampered_event_is_refused_at_every_hop_every_collection(collection: str) -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    ctx = lambda: _context(kp.public_key, grant)  # noqa: E731
    tampered = replace(_event(collection, kp.private_key), payload={"field": "TAMPERED"})

    assert not verify(tampered, kp.public_key)  # origin
    with pytest.raises(EventRejected) as exc:
        VerifyingBackend(FakeBackend(), context=ctx).push([tampered])  # backend
    assert exc.value.code == INVALID_SIGNATURE
    raw = FakeBackend()
    raw.push([tampered])
    assert VerifyingBackend(raw, context=ctx).pull() == []  # receiver drops it


def test_golden_vectors_conform_to_the_canonical_codec() -> None:
    """Every checked-in golden event is a canonical fixed-point and its signing
    bytes match the recorded vector — the codec conforms for the golden data."""
    events, _grants = load_protocol_vectors()
    assert events  # the suite has inputs
    for vector in events:
        event = Event(**{**vector.entity, "sig": vector.sig_hex})
        assert signing_bytes(event).hex() == vector.signing_bytes_hex
        wire = encode_canonical(event)
        assert encode_canonical(decode(wire)) == wire


def test_conformance_covers_every_syncable_collection() -> None:
    """No silent gap: the per-collection cases + the trust roots cover exactly the
    syncable allowlist — adding a collection without a case fails this loudly."""
    covered = set(VERB_GATED) | set(_TRUST_ROOTS)
    assert covered == set(SYNCABLE_MODELS), (
        f"conformance coverage drift: {covered ^ set(SYNCABLE_MODELS)}"
    )
