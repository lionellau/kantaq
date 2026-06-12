"""Offline grant verification: valid accepted, everything else named (E03-T3)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kantaq_protocol import (
    GRANT_EXPIRED,
    GRANT_FORGED,
    GRANT_INVALID_VALIDITY,
    GRANT_MISSING_SIGNATURE,
    GRANT_NOT_YET_VALID,
    GRANT_OK,
    GRANT_UNKNOWN_ROOT,
    CapabilityGrant,
    SchemaViolation,
    generate_keypair,
    sign_grant,
    verify_grant,
)

T0 = 1_767_225_600  # 2026-01-01T00:00:00Z
HOUR = 3600

DEVICE_ID = "01JDEVICE00000000000000001"
KEYS = generate_keypair()
ROOTS = {DEVICE_ID: KEYS.public_key}


def _grant(**overrides: object) -> CapabilityGrant:
    base: dict[str, object] = {
        "grant_id": "01JGRANT000000000000000001",
        "subject": "01JMEMBER00000000000000001",
        "issuer": DEVICE_ID,
        "resource": "workspace/01JWS0000000000000000001",
        "verbs": ("tickets.read", "tickets.write"),
        "issued_at": T0,
        "expires_at": T0 + HOUR,
    }
    base.update(overrides)
    return CapabilityGrant(**base)  # type: ignore[arg-type]


def test_a_valid_grant_verifies_offline() -> None:
    signed = sign_grant(_grant(), KEYS.private_key)
    result = verify_grant(signed, ROOTS, now=T0 + 60)
    assert result.ok
    assert result.reason == GRANT_OK
    assert bool(result) is True


def test_an_unsigned_grant_is_refused() -> None:
    result = verify_grant(_grant(), ROOTS, now=T0 + 60)
    assert not result.ok
    assert result.reason == GRANT_MISSING_SIGNATURE


def test_an_expired_grant_is_refused() -> None:
    signed = sign_grant(_grant(), KEYS.private_key)
    result = verify_grant(signed, ROOTS, now=T0 + HOUR)  # exactly at expiry: dead
    assert result.reason == GRANT_EXPIRED


def test_a_not_yet_valid_grant_is_refused() -> None:
    signed = sign_grant(_grant(), KEYS.private_key)
    assert verify_grant(signed, ROOTS, now=T0 - 1).reason == GRANT_NOT_YET_VALID


def test_a_tampered_grant_is_forged() -> None:
    signed = sign_grant(_grant(), KEYS.private_key)
    widened = replace(signed, verbs=(*signed.verbs, "members.revoke"))
    assert verify_grant(widened, ROOTS, now=T0 + 60).reason == GRANT_FORGED


def test_a_stretched_expiry_is_forged() -> None:
    signed = sign_grant(_grant(), KEYS.private_key)
    stretched = replace(signed, expires_at=T0 + 24 * HOUR)
    assert verify_grant(stretched, ROOTS, now=T0 + 60).reason == GRANT_FORGED


def test_an_unknown_issuer_is_refused_before_crypto() -> None:
    signed = sign_grant(_grant(issuer="01JROGUE000000000000000001"), KEYS.private_key)
    roots = {DEVICE_ID: KEYS.public_key}
    assert verify_grant(signed, roots, now=T0 + 60).reason == GRANT_UNKNOWN_ROOT


def test_a_wrong_root_key_is_forged() -> None:
    other = generate_keypair()
    signed = sign_grant(_grant(), KEYS.private_key)
    assert verify_grant(signed, {DEVICE_ID: other.public_key}, now=T0 + 60).reason == GRANT_FORGED


def test_inverted_validity_is_structurally_invalid() -> None:
    signed = sign_grant(_grant(), KEYS.private_key)
    inverted = replace(signed, issued_at=T0 + HOUR, expires_at=T0)
    assert verify_grant(inverted, ROOTS, now=T0).reason == GRANT_INVALID_VALIDITY


def test_forged_beats_expired_in_reporting() -> None:
    # An expired AND tampered grant reports forged — audit must not undersell.
    signed = sign_grant(_grant(), KEYS.private_key)
    tampered = replace(signed, subject="01JATTACKER000000000000001")
    assert verify_grant(tampered, ROOTS, now=T0 + 2 * HOUR).reason == GRANT_FORGED


def test_grant_without_verbs_is_rejected() -> None:
    with pytest.raises(SchemaViolation, match="at least one verb"):
        sign_grant(_grant(verbs=()), KEYS.private_key)


def test_revokes_field_is_signed() -> None:
    signed = sign_grant(_grant(revokes="01JOLDGRANT000000000000001"), KEYS.private_key)
    assert verify_grant(signed, ROOTS, now=T0 + 60).ok
    unlinked = replace(signed, revokes=None)
    assert verify_grant(unlinked, ROOTS, now=T0 + 60).reason == GRANT_FORGED


def test_empty_grant_fields_are_rejected() -> None:
    with pytest.raises(SchemaViolation, match="'subject' must be non-empty"):
        sign_grant(_grant(subject=""), KEYS.private_key)


def test_the_wire_form_includes_the_signature() -> None:
    from kantaq_protocol import encode_canonical_grant, grant_signing_bytes

    signed = sign_grant(_grant(), KEYS.private_key)
    assert signed.sig is not None
    wire = encode_canonical_grant(signed)
    assert signed.sig.encode() in wire
    assert grant_signing_bytes(signed) == grant_signing_bytes(_grant())
