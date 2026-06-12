"""Ed25519 sign/verify + golden vectors, cross-verified (E03-T2, D-11).

The Crypto profile's load-bearing suite: the checked-in golden vectors must
pass under BOTH pyca/cryptography (the chosen library) and PyNaCl/libsodium
(the independent second implementation), and the RFC 8032 §7.1 vectors must
pass under both too — so the vectors are grounded in the standard, not in
either library's opinion of itself. One flipped byte anywhere fails verify.
"""

from __future__ import annotations

import nacl.exceptions
import nacl.signing
import pytest

from kantaq_protocol import (
    CapabilityGrant,
    Event,
    SchemaViolation,
    decode,
    encode_canonical,
    generate_keypair,
    grant_signing_bytes,
    public_key_of,
    sign,
    sign_bytes,
    signing_bytes,
    verify,
    verify_bytes,
)
from kantaq_test_harness.vectors import (
    load_protocol_vectors,
    load_rfc8032_vectors,
)

EVENT_VECTORS, GRANT_VECTORS = load_protocol_vectors()
RFC_VECTORS = load_rfc8032_vectors()


def _nacl_verifies(message: bytes, sig_hex: str, public_key_hex: str) -> bool:
    try:
        nacl.signing.VerifyKey(bytes.fromhex(public_key_hex)).verify(
            message, bytes.fromhex(sig_hex)
        )
    except nacl.exceptions.BadSignatureError:
        return False
    return True


def _event_from(entity: dict[str, object]) -> Event:
    return Event(**entity)  # type: ignore[arg-type]


# ------------------------------------------------------------ basic contract


def test_sign_then_verify_round_trip() -> None:
    keys = generate_keypair()
    event = _event_from(EVENT_VECTORS[0].entity)
    signed = sign(event, keys.private_key)
    assert signed.sig is not None
    assert verify(signed, keys.public_key)


def test_unsigned_event_never_verifies() -> None:
    keys = generate_keypair()
    assert verify(_event_from(EVENT_VECTORS[0].entity), keys.public_key) is False


def test_wrong_key_fails_verify() -> None:
    signer, other = generate_keypair(), generate_keypair()
    signed = sign(_event_from(EVENT_VECTORS[0].entity), signer.private_key)
    assert verify(signed, other.public_key) is False


def test_malformed_signature_hex_fails_closed() -> None:
    keys = generate_keypair()
    event = sign(_event_from(EVENT_VECTORS[0].entity), keys.private_key)
    assert verify_bytes(signing_bytes(event), "zz-not-hex", keys.public_key) is False


def test_garbage_keys_raise_schema_violation() -> None:
    with pytest.raises(SchemaViolation):
        sign_bytes(b"m", "abcd")  # 2 bytes, not a 32-byte seed
    with pytest.raises(SchemaViolation):
        verify_bytes(b"m", "ab" * 64, "not-a-key")


def test_public_key_of_matches_generate() -> None:
    keys = generate_keypair()
    assert public_key_of(keys.private_key) == keys.public_key


# ----------------------------------------------------------- flipped bytes


def test_one_flipped_payload_byte_fails_verify() -> None:
    keys = generate_keypair()
    signed = sign(_event_from(EVENT_VECTORS[0].entity), keys.private_key)
    canonical = bytearray(encode_canonical(signed))
    # Flip one bit in every byte position of the signed region in turn —
    # any single corruption must break either decode or verify.
    survivors = []
    for index in range(len(canonical)):
        tampered = bytearray(canonical)
        tampered[index] ^= 0x01
        try:
            mutated = decode(bytes(tampered))
        except SchemaViolation:
            continue  # corruption broke the canonical form itself: fail closed
        if verify(mutated, keys.public_key):
            survivors.append(index)
    assert survivors == []


def test_one_flipped_signature_byte_fails_verify() -> None:
    keys = generate_keypair()
    signed = sign(_event_from(EVENT_VECTORS[0].entity), keys.private_key)
    assert signed.sig is not None
    sig = bytearray(bytes.fromhex(signed.sig))
    sig[0] ^= 0x01
    assert verify_bytes(signing_bytes(signed), sig.hex(), keys.public_key) is False


# ----------------------------------------------------------- golden vectors


@pytest.mark.parametrize("vector", EVENT_VECTORS, ids=lambda v: v.name)
def test_event_golden_vectors_pass_with_the_chosen_library(vector) -> None:  # type: ignore[no-untyped-def]
    event = _event_from(vector.entity)
    message = signing_bytes(event)
    assert message.hex() == vector.signing_bytes_hex, "canonical encoding drifted (NFR-E03-1)"
    assert verify_bytes(message, vector.sig_hex, vector.public_key_hex)
    # Ed25519 is deterministic: re-signing reproduces the exact signature.
    assert sign(event, vector.private_key_hex).sig == vector.sig_hex


@pytest.mark.parametrize("vector", EVENT_VECTORS, ids=lambda v: v.name)
def test_event_golden_vectors_cross_verify_with_pynacl(vector) -> None:  # type: ignore[no-untyped-def]
    assert _nacl_verifies(
        bytes.fromhex(vector.signing_bytes_hex), vector.sig_hex, vector.public_key_hex
    )


@pytest.mark.parametrize("vector", GRANT_VECTORS, ids=lambda v: v.name)
def test_grant_golden_vectors_pass_with_both_libraries(vector) -> None:  # type: ignore[no-untyped-def]
    grant = CapabilityGrant(
        **{**vector.entity, "verbs": tuple(vector.entity["verbs"])}  # type: ignore[arg-type]
    )
    message = grant_signing_bytes(grant)
    assert message.hex() == vector.signing_bytes_hex
    assert verify_bytes(message, vector.sig_hex, vector.public_key_hex)
    assert _nacl_verifies(message, vector.sig_hex, vector.public_key_hex)


@pytest.mark.parametrize("vector", EVENT_VECTORS, ids=lambda v: v.name)
def test_tampering_any_golden_event_field_fails_both_libraries(vector) -> None:  # type: ignore[no-untyped-def]
    event = _event_from(vector.entity)
    tampered = Event(
        **{**vector.entity, "actor_seq": int(vector.entity["actor_seq"]) + 1}  # type: ignore[arg-type]
    )
    assert signing_bytes(tampered) != signing_bytes(event)
    assert not verify_bytes(signing_bytes(tampered), vector.sig_hex, vector.public_key_hex)
    assert not _nacl_verifies(signing_bytes(tampered), vector.sig_hex, vector.public_key_hex)


# ------------------------------------------------------------------ RFC 8032


@pytest.mark.parametrize("vector", RFC_VECTORS, ids=lambda v: v.name)
def test_rfc8032_vectors_pass_with_the_chosen_library(vector) -> None:  # type: ignore[no-untyped-def]
    message = bytes.fromhex(vector.message_hex)
    assert public_key_of(vector.private_key_hex) == vector.public_key_hex
    assert sign_bytes(message, vector.private_key_hex) == vector.sig_hex
    assert verify_bytes(message, vector.sig_hex, vector.public_key_hex)


@pytest.mark.parametrize("vector", RFC_VECTORS, ids=lambda v: v.name)
def test_rfc8032_vectors_pass_with_pynacl(vector) -> None:  # type: ignore[no-untyped-def]
    message = bytes.fromhex(vector.message_hex)
    signing_key = nacl.signing.SigningKey(bytes.fromhex(vector.private_key_hex))
    assert signing_key.sign(message).signature.hex() == vector.sig_hex
    assert _nacl_verifies(message, vector.sig_hex, vector.public_key_hex)
