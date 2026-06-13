"""E24-T5 — verified ingestion: the deny matrix + the VerifyingBackend gate.

Every check fails closed with the right structured code, beside a positive
control; the wrapper rejects on push (atomic — nothing submitted) and drops on
pull (fail closed — never folded), writing a denial each time. Hermetic:
SeededRandom is not even needed because the keypairs come from the OS CSPRNG
and the assertions are over the verdict, not the bytes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kantaq_protocol import CapabilityGrant, generate_keypair, sign, sign_grant
from kantaq_sync_engine import (
    INVALID_SIGNATURE,
    POLICY_DENIED,
    SCHEMA_VIOLATION,
    SYNCABLE_MODELS,
    UNSIGNED,
    VERIFY_OK,
    Event,
    EventRejected,
    VerifyContext,
    VerifyingBackend,
    verify_event,
)
from kantaq_test_harness.backend import FakeBackend

NOW = 1_767_225_600  # a fixed instant inside the grant window
DEVICE = "dev_alice".ljust(26, "0")
MEMBER = "mbr_alice".ljust(26, "0")
GRANT = "grt_self".ljust(26, "0")


def _grant(private_key: str, **overrides: object) -> CapabilityGrant:
    base = CapabilityGrant(
        grant_id=GRANT,
        subject=MEMBER,
        issuer=DEVICE,
        resource="workspace/ws_a",
        verbs=("tickets.write",),
        issued_at=NOW - 60,
        expires_at=NOW + 3600,
    )
    return sign_grant(replace(base, **overrides), private_key)


def _event(private_key: str, seq: int = 1, *, sign_it: bool = True, **overrides: object) -> Event:
    base = Event(
        event_id=f"evt{seq:023d}",
        collection="tickets",
        entity_id="tkt_1".ljust(26, "0"),
        actor_id=MEMBER,
        actor_seq=seq,
        policy_ref=GRANT,
        payload={"title": f"v{seq}"},
    )
    base = replace(base, **overrides)
    return sign(base, private_key) if sign_it else base


def _context(public_key: str, grant: CapabilityGrant, **overrides: object) -> VerifyContext:
    defaults: dict[str, object] = {
        "roots": {DEVICE: public_key},
        "grants": {grant.grant_id: grant},
        "now": NOW,
    }
    defaults.update(overrides)
    return VerifyContext(**defaults)  # type: ignore[arg-type]


# ----------------------------------------------------------- the deny matrix


def test_positive_control_verifies() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    verdict = verify_event(_event(kp.private_key), _context(kp.public_key, grant))
    assert verdict.ok and verdict.code == VERIFY_OK


def test_unsigned_event_is_rejected() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    verdict = verify_event(_event(kp.private_key, sign_it=False), _context(kp.public_key, grant))
    assert not verdict.ok and verdict.code == UNSIGNED


def test_unsigned_is_accepted_before_the_cutover() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    ctx = _context(kp.public_key, grant, require_signature=False)
    assert verify_event(_event(kp.private_key, sign_it=False), ctx).ok


def test_tampered_byte_breaks_the_signature() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    signed = _event(kp.private_key)
    tampered = replace(signed, payload={"title": "TAMPERED"})  # sig now stale
    verdict = verify_event(tampered, _context(kp.public_key, grant))
    assert not verdict.ok and verdict.code == INVALID_SIGNATURE


def test_missing_grant_is_policy_denied() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    # policy_ref names a grant the store does not hold.
    event = _event(kp.private_key, policy_ref="grt_unknown".ljust(26, "0"))
    verdict = verify_event(event, _context(kp.public_key, grant))
    assert not verdict.ok and verdict.code == POLICY_DENIED


def test_forged_grant_is_policy_denied() -> None:
    kp = generate_keypair()
    forger = generate_keypair()
    # Grant claims issuer DEVICE but is signed by a key DEVICE's root is not.
    forged = _grant(forger.private_key)
    event = _event(kp.private_key)
    verdict = verify_event(event, _context(kp.public_key, forged))
    assert not verdict.ok and verdict.code == POLICY_DENIED
    assert "forged" in verdict.reason


def test_unknown_issuer_root_is_policy_denied() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    ctx = _context(kp.public_key, grant, roots={})  # no device roots at all
    verdict = verify_event(_event(kp.private_key), ctx)
    assert not verdict.ok and verdict.code == POLICY_DENIED


def test_expired_grant_is_policy_denied() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    ctx = _context(kp.public_key, grant, now=grant.expires_at + 1)
    verdict = verify_event(_event(kp.private_key), ctx)
    assert not verdict.ok and verdict.code == POLICY_DENIED
    assert "expired" in verdict.reason


def test_revoked_grant_is_policy_denied() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    ctx = _context(kp.public_key, grant, revoked_ids={GRANT})
    verdict = verify_event(_event(kp.private_key), ctx)
    assert not verdict.ok and verdict.code == POLICY_DENIED
    assert "revoked" in verdict.reason


def test_grant_subject_must_match_the_actor() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    # A properly signed event from a different actor, under MEMBER's grant.
    event = _event(kp.private_key, actor_id="mbr_intruder".ljust(26, "0"))
    verdict = verify_event(event, _context(kp.public_key, grant))
    assert not verdict.ok and verdict.code == POLICY_DENIED
    assert "subject" in verdict.reason


def test_grant_verbs_must_authorise_the_collection() -> None:
    """E27 fix: a narrow grant cannot ride a write to a collection it does not
    cover — the fine per-verb scoping D-03 assigns to grants."""
    kp = generate_keypair()
    # A grant scoped to memory only, used to sign a tickets event.
    grant = _grant(kp.private_key, verbs=("memory.write",))
    verdict = verify_event(_event(kp.private_key), _context(kp.public_key, grant))
    assert not verdict.ok and verdict.code == POLICY_DENIED
    assert "tickets" in verdict.reason


def test_malformed_event_is_a_schema_violation_not_a_crash() -> None:
    """E27 fix: a poisoned remote event (non-canonical payload) is dropped as
    schema_violation — it must never raise and wedge the pull loop."""
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    # A float is not canonically encodable; pair it with a syntactically-valid
    # but bogus signature so step 1 (signed?) passes and we reach the codec.
    poisoned = Event(
        event_id="evt".ljust(26, "0"),
        collection="tickets",
        entity_id="tkt_1".ljust(26, "0"),
        actor_id=MEMBER,
        actor_seq=9,
        policy_ref=GRANT,
        payload={"bad": 1.5},
        sig="a" * 128,
    )
    verdict = verify_event(poisoned, _context(kp.public_key, grant))  # does not raise
    assert not verdict.ok and verdict.code == SCHEMA_VIOLATION


def test_trust_root_tables_never_ride_the_fold() -> None:
    """The signature gate reduces to the integrity of devices + grants, so they
    (and tokens + the local audit trail) must never be syncable collections
    a pulled event could forge into existence (E27 precondition, MED-4)."""
    for collection in ("devices", "capability_grants", "tokens", "audit_events"):
        assert collection not in SYNCABLE_MODELS


# ----------------------------------------------------- the VerifyingBackend


def test_push_rejects_an_unverifiable_event_atomically() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    fake = FakeBackend()
    denied: list[str] = []
    backend = VerifyingBackend(
        fake,
        context=lambda: _context(kp.public_key, grant),
        on_deny=lambda event, verdict: denied.append(verdict.code),
    )
    backend.push([_event(kp.private_key, 1)])  # a good one commits
    with pytest.raises(EventRejected) as excinfo:
        backend.push([_event(kp.private_key, 2, sign_it=False)])
    assert excinfo.value.code == UNSIGNED
    assert denied == [UNSIGNED]
    # Atomic: the rejected batch committed nothing — only the first event is there.
    assert [entry.event.actor_seq for entry in fake.pull()] == [1]


def test_pull_drops_unverifiable_events_and_audits() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    fake = FakeBackend()
    # Seed the shared log directly (bypassing the gate, like a tampered peer).
    fake.push([_event(kp.private_key, 1)])  # good
    fake.push([_event(kp.private_key, 2, sign_it=False)])  # unsigned
    fake.push([replace(_event(kp.private_key, 3), payload={"x": "tamper"})])  # tampered

    denied: list[str] = []
    backend = VerifyingBackend(
        fake,
        context=lambda: _context(kp.public_key, grant),
        on_deny=lambda event, verdict: denied.append(verdict.code),
    )
    kept = backend.pull()
    assert [entry.event.actor_seq for entry in kept] == [1]  # only the verified one folds
    assert sorted(denied) == sorted([UNSIGNED, INVALID_SIGNATURE])


def test_pre_cutover_history_passes_through_unverified() -> None:
    kp = generate_keypair()
    grant = _grant(kp.private_key)
    fake = FakeBackend()
    fake.push([_event(kp.private_key, 1, sign_it=False)])  # unsigned, but rev 1
    backend = VerifyingBackend(fake, context=lambda: _context(kp.public_key, grant), cutover_rev=1)
    # rev 1 <= cutover_rev → immutable pre-cutover history, kept despite no sig.
    assert [entry.revision for entry in backend.pull()] == [1]
