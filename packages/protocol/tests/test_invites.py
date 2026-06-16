"""The signed twp://invite bundle (E06-T8, DEBT-04): sign/verify, URI, refusals.

The grant-verifier discipline applied to onboarding: an invite verifies offline
against the issuer device's root, a tampered or expired or wrong-root invite is
refused with a named reason, the URI round-trips byte-exactly, and the invite
domain is separated from the grant domain (no cross-type replay).
"""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from kantaq_protocol import (
    INVITE_EXPIRED,
    INVITE_FORGED,
    INVITE_INVALID_VALIDITY,
    INVITE_MISSING_SIGNATURE,
    INVITE_NOT_YET_VALID,
    INVITE_OK,
    INVITE_UNKNOWN_ROOT,
    Invite,
    SchemaViolation,
    decode_invite_uri,
    encode_invite_uri,
    generate_keypair,
    sign_invite,
    verify_invite,
)

ISSUER = "dev_alice0000000000000000"


def _invite(**over: object) -> Invite:
    base = {
        "invite_id": "inv_0000000000000000000001",
        "workspace_id": "ws_main00000000000000000",
        "subject_email": "bob@acme.dev",
        "role": "Member",
        "resource": "workspace/main",
        "verbs": ("tickets.read", "tickets.write"),
        "issuer": ISSUER,
        "issued_at": 1_000_000,
        "expires_at": 1_000_000 + 7 * 86_400,
    }
    base.update(over)
    return Invite(**base)  # type: ignore[arg-type]


def test_sign_then_verify_against_the_issuer_root() -> None:
    kp = generate_keypair()
    signed = sign_invite(_invite(), kp.private_key)
    assert signed.sig is not None
    result = verify_invite(signed, {ISSUER: kp.public_key}, now=1_000_100)
    assert result.ok and result.reason == INVITE_OK


def test_uri_round_trip_is_byte_exact() -> None:
    kp = generate_keypair()
    signed = sign_invite(_invite(), kp.private_key)
    uri = encode_invite_uri(signed)
    assert uri.startswith("twp://invite/")
    assert decode_invite_uri(uri) == signed


def test_a_tampered_invite_is_forged() -> None:
    kp = generate_keypair()
    signed = sign_invite(_invite(), kp.private_key)
    # Widen the grant after signing — the signature no longer covers it.
    tampered = replace(signed, verbs=("tickets.read", "tickets.write", "members.invite"))
    assert verify_invite(tampered, {ISSUER: kp.public_key}, now=1_000_100).reason == INVITE_FORGED


def test_an_unknown_issuer_root_is_refused() -> None:
    kp = generate_keypair()
    signed = sign_invite(_invite(), kp.private_key)
    assert verify_invite(signed, {"dev_someone_else": kp.public_key}, now=1_000_100).reason == (
        INVITE_UNKNOWN_ROOT
    )


def test_expiry_and_not_yet_valid() -> None:
    kp = generate_keypair()
    signed = sign_invite(_invite(), kp.private_key)
    roots = {ISSUER: kp.public_key}
    assert verify_invite(signed, roots, now=signed.expires_at).reason == INVITE_EXPIRED
    assert verify_invite(signed, roots, now=signed.issued_at - 1).reason == INVITE_NOT_YET_VALID


def test_missing_signature_and_invalid_validity() -> None:
    kp = generate_keypair()
    roots = {ISSUER: kp.public_key}
    assert verify_invite(_invite(), roots, now=1_000_100).reason == INVITE_MISSING_SIGNATURE
    # expires <= issued is refused before anything else (a signature can't fix it).
    bad = sign_invite(_invite(expires_at=1_000_000), kp.private_key)
    assert verify_invite(bad, roots, now=1_000_100).reason == INVITE_INVALID_VALIDITY


def test_a_forged_signature_outranks_expiry_in_the_reason() -> None:
    """Honest audit: a forged + expired invite reports forged, not expired."""
    kp = generate_keypair()
    tampered = replace(sign_invite(_invite(), kp.private_key), subject_email="evil@acme.dev")
    assert verify_invite(tampered, {ISSUER: kp.public_key}, now=tampered.expires_at + 1).reason == (
        INVITE_FORGED
    )


def test_a_grant_signature_cannot_pose_as_an_invite() -> None:
    """Domain separation: an invite signed-bytes carry the kantaq:invite:v1 tag,
    so a signature minted over any other domain fails closed."""
    from kantaq_protocol import grant_signing_bytes
    from kantaq_protocol.entities import CapabilityGrant
    from kantaq_protocol.signing import sign_bytes

    kp = generate_keypair()
    inv = _invite()
    # Sign the GRANT domain bytes of a look-alike grant, then graft onto the invite.
    grant = CapabilityGrant(
        grant_id=inv.invite_id,
        subject=inv.subject_email,
        issuer=inv.issuer,
        resource=inv.resource,
        verbs=inv.verbs,
        issued_at=inv.issued_at,
        expires_at=inv.expires_at,
    )
    cross = replace(inv, sig=sign_bytes(grant_signing_bytes(grant), kp.private_key))
    assert verify_invite(cross, {ISSUER: kp.public_key}, now=1_000_100).reason == INVITE_FORGED


def test_malformed_uris_are_refused() -> None:
    with pytest.raises(SchemaViolation):
        decode_invite_uri("https://evil.example/invite/x")
    with pytest.raises(SchemaViolation):
        decode_invite_uri("twp://invite/")
    with pytest.raises(SchemaViolation):
        decode_invite_uri("twp://invite/" + base64.urlsafe_b64encode(b"not-canonical").decode())
