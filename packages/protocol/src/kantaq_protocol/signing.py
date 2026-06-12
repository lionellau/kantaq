"""Ed25519 sign/verify and key generation (FR-E03-3, RFC 8032).

Implementation: **pyca/cryptography** (the E03 golden-rule pick — see
docs/stack.md; PyNaCl/libsodium is the independent second implementation the
golden vectors are cross-verified against, D-11, as a test-only dependency).

Key and signature representation is **strict lowercase hex** end to end: a
32-byte seed for private keys, a 32-byte verify key, a 64-byte signature —
the same encoding RFC 8032 §7.1 publishes its test vectors in. Strictness is
load-bearing, not cosmetic: ``bytes.fromhex`` alone accepts uppercase and
embedded whitespace, which would let two different canonical byte strings
carry one valid signature (adversarial review, must-fix).

The private key never appears inside any entity, and ``KeyPair`` hides it
from ``repr`` so a stray log line cannot print a seed. It lives in the
runtime keychain (D-01, MOD-06); only signatures travel.

``verify`` takes the public key explicitly: resolving *whose* key applies is
identity's job (the devices registry, MOD-06), not the codec's.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from kantaq_protocol.canonical import KEY_HEX, SIG_HEX, signing_bytes
from kantaq_protocol.entities import Event
from kantaq_protocol.errors import SchemaViolation


@dataclass(frozen=True, slots=True)
class KeyPair:
    """One Ed25519 keypair, hex-encoded. The private seed never reprs."""

    private_key: str = field(repr=False)
    public_key: str = ""


def generate_keypair() -> KeyPair:
    """A fresh Ed25519 keypair from the OS CSPRNG."""
    private = Ed25519PrivateKey.generate()
    return KeyPair(
        private_key=private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex(),
        public_key=private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
    )


def _private_from_hex(private_key_hex: str) -> Ed25519PrivateKey:
    if not isinstance(private_key_hex, str) or KEY_HEX.match(private_key_hex) is None:
        raise SchemaViolation("an Ed25519 private key seed is 64 lowercase hex characters")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))


def _public_from_hex(public_key_hex: str) -> Ed25519PublicKey:
    if not isinstance(public_key_hex, str) or KEY_HEX.match(public_key_hex) is None:
        raise SchemaViolation("an Ed25519 public key is 64 lowercase hex characters")
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))


def public_key_of(private_key_hex: str) -> str:
    """The hex verify key for a hex seed (registration helper, MOD-06)."""
    private = _private_from_hex(private_key_hex)
    return private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def sign_bytes(message: bytes, private_key_hex: str) -> str:
    """Hex Ed25519 signature over raw bytes (shared by events and grants)."""
    return _private_from_hex(private_key_hex).sign(message).hex()


def verify_bytes(message: bytes, signature_hex: str, public_key_hex: str) -> bool:
    """True iff the strict-hex signature is valid for these bytes under this key.

    A malleable signature spelling (uppercase, whitespace, wrong length) is
    simply invalid — there is exactly one accepted encoding per signature.
    """
    public = _public_from_hex(public_key_hex)
    if not isinstance(signature_hex, str) or SIG_HEX.match(signature_hex) is None:
        return False
    try:
        public.verify(bytes.fromhex(signature_hex), message)
    except InvalidSignature:
        return False
    return True


def sign(event: Event, private_key_hex: str) -> Event:
    """A copy of the event with ``sig`` set over its domain-separated signing bytes."""
    return replace(event, sig=sign_bytes(signing_bytes(event), private_key_hex))


def verify(event: Event, public_key_hex: str) -> bool:
    """True iff the event's ``sig`` matches its domain-separated signing bytes.

    Fail closed: an unsigned event never verifies. One flipped byte anywhere
    in the signed fields changes the canonical form and fails (test-pinned).
    """
    if event.sig is None:
        return False
    return verify_bytes(signing_bytes(event), event.sig, public_key_hex)
