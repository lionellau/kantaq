"""Capability-grant codec and offline verification (FR-E03-4, E03-T3).

A grant verifies against a set of **roots** — the registered device verify
keys (MOD-06's ``devices`` table) — with no network and no clock authority
beyond the caller's ``now``: a teammate's runtime can check a grant on a
plane. Verification answers *was this permission slip issued by a known
device, is it currently valid, and has the store revoked it* — and returns a
structured result, never a bare bool, so callers and audit rows can say why
a grant was refused.

Hardening from the adversarial review (E27 security gate):

- **Strict runtime schema** before any sign/verify: field types are checked
  (a ``verbs`` *string* would make ``"x" in grant.verbs`` substring-true
  downstream; bool timestamps would sign as ``true``/``false``).
- **Strict decode** (``decode_grant``): grants get the same byte-level
  canonical gate events get — duplicate keys, null-vs-omitted, and
  non-canonical spellings cannot reach a verifier as Python objects.
- **Domain separation**: a grant signs ``kantaq:grant:v1 NUL <canonical>``,
  so a grant signature can never validate as an event signature or vice
  versa, independent of schema shape.
- **Revocation input**: the store's revoked grant ids are an explicit
  parameter; a signature cannot prove an absence, so an API that only checks
  signature + time must not look like an authorization check.
"""

from __future__ import annotations

from collections.abc import Collection as SizedCollection
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from kantaq_protocol.canonical import (
    SIG_HEX,
    canonicalize,
    parse_canonical_document,
)
from kantaq_protocol.entities import CapabilityGrant
from kantaq_protocol.errors import SchemaViolation
from kantaq_protocol.signing import sign_bytes, verify_bytes

# Domain-separation tag (see canonical.EVENT_SIGNING_DOMAIN).
GRANT_SIGNING_DOMAIN = b"kantaq:grant:v1\x00"

# Structured refusal reasons (wire vocabulary, like the FR-E03-5 codes).
GRANT_OK = "ok"
GRANT_MISSING_SIGNATURE = "missing_signature"
GRANT_FORGED = "forged"
GRANT_UNKNOWN_ROOT = "unknown_root"
GRANT_EXPIRED = "expired"
GRANT_NOT_YET_VALID = "not_yet_valid"
GRANT_INVALID_VALIDITY = "invalid_validity"
GRANT_REVOKED = "revoked"

_GRANT_REQUIRED = (
    "grant_id",
    "subject",
    "issuer",
    "resource",
    "verbs",
    "issued_at",
    "expires_at",
)
_GRANT_FIELDS = (*_GRANT_REQUIRED, "revokes", "sig")


@dataclass(frozen=True, slots=True)
class GrantVerification:
    """The outcome of one offline check: ``ok`` or a named refusal."""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_grant(grant: CapabilityGrant) -> None:
    """Strict runtime schema, shared by encode, sign, verify, and decode."""
    for name in ("grant_id", "subject", "issuer", "resource"):
        value = getattr(grant, name)
        if not isinstance(value, str) or not value:
            raise SchemaViolation(f"grant field {name!r} must be a non-empty string")
    verbs = grant.verbs
    if isinstance(verbs, str | bytes) or not isinstance(verbs, list | tuple) or not verbs:
        raise SchemaViolation("grant field 'verbs' must be a non-empty list of strings")
    for verb in verbs:
        if not isinstance(verb, str) or not verb:
            raise SchemaViolation("grant field 'verbs' must contain only non-empty strings")
    for name in ("issued_at", "expires_at"):
        if not _is_int(getattr(grant, name)):
            raise SchemaViolation(f"grant field {name!r} must be an integer (unix seconds)")
    if grant.revokes is not None and (not isinstance(grant.revokes, str) or not grant.revokes):
        raise SchemaViolation("grant field 'revokes' must be a non-empty string")
    if grant.sig is not None and (
        not isinstance(grant.sig, str) or SIG_HEX.match(grant.sig) is None
    ):
        raise SchemaViolation("grant field 'sig' must be 128 lowercase hex characters")


def _grant_mapping(grant: CapabilityGrant, *, include_sig: bool) -> dict[str, Any]:
    _validate_grant(grant)
    mapping: dict[str, Any] = {
        "grant_id": grant.grant_id,
        "subject": grant.subject,
        "issuer": grant.issuer,
        "resource": grant.resource,
        "verbs": list(grant.verbs),
        "issued_at": grant.issued_at,
        "expires_at": grant.expires_at,
    }
    if grant.revokes is not None:
        mapping["revokes"] = grant.revokes
    if include_sig and grant.sig is not None:
        mapping["sig"] = grant.sig
    return mapping


def grant_signing_bytes(grant: CapabilityGrant) -> bytes:
    """What the issuer's device signs: the domain tag + canonical grant minus ``sig``."""
    return GRANT_SIGNING_DOMAIN + canonicalize(_grant_mapping(grant, include_sig=False))


def encode_canonical_grant(grant: CapabilityGrant) -> bytes:
    """The grant's full canonical wire form (includes ``sig`` when present)."""
    return canonicalize(_grant_mapping(grant, include_sig=True))


def decode_grant(data: bytes) -> CapabilityGrant:
    """Parse canonical bytes back into a ``CapabilityGrant`` (strict, fail closed).

    The grant-side twin of ``canonical.decode``: unknown fields, missing
    fields, wrong types, malleable signature spellings, and non-canonical
    input are all refused, so only one byte spelling of a grant exists.
    """
    raw = parse_canonical_document(data)
    unknown = set(raw) - set(_GRANT_FIELDS)
    if unknown:
        raise SchemaViolation(f"unknown grant fields: {sorted(unknown)}")
    missing = set(_GRANT_REQUIRED) - set(raw)
    if missing:
        raise SchemaViolation(f"missing grant fields: {sorted(missing)}")
    if not isinstance(raw["verbs"], list):
        raise SchemaViolation("grant field 'verbs' must be a list")
    grant = CapabilityGrant(
        grant_id=raw["grant_id"],
        subject=raw["subject"],
        issuer=raw["issuer"],
        resource=raw["resource"],
        verbs=tuple(raw["verbs"]),
        issued_at=raw["issued_at"],
        expires_at=raw["expires_at"],
        revokes=raw.get("revokes"),
        sig=raw.get("sig"),
    )
    if encode_canonical_grant(grant) != data:
        raise SchemaViolation(
            "input is not in canonical form (re-encoding differs); "
            "only canonical bytes are accepted"
        )
    return grant


def sign_grant(grant: CapabilityGrant, private_key_hex: str) -> CapabilityGrant:
    """A copy of the grant signed by the issuing device's key."""
    return replace(grant, sig=sign_bytes(grant_signing_bytes(grant), private_key_hex))


def verify_grant(
    grant: CapabilityGrant,
    roots: Mapping[str, str],
    *,
    now: int,
    revoked_ids: SizedCollection[str] = (),
) -> GrantVerification:
    """Offline check: signature against a known root, validity against ``now``.

    ``roots`` maps issuer (device) id -> hex Ed25519 verify key. ``now`` is
    unix seconds UTC. ``revoked_ids`` is the store's knowledge of revoked
    grants (MOD-06 supplies it; a signature cannot prove an absence). Order
    matters for honest audit reasons: a forged signature reports forged even
    when the grant is also expired or revoked.
    """
    _validate_grant(grant)
    if grant.expires_at <= grant.issued_at:
        return GrantVerification(False, GRANT_INVALID_VALIDITY)
    if grant.sig is None:
        return GrantVerification(False, GRANT_MISSING_SIGNATURE)
    root_key = roots.get(grant.issuer)
    if root_key is None:
        return GrantVerification(False, GRANT_UNKNOWN_ROOT)
    if not verify_bytes(grant_signing_bytes(grant), grant.sig, root_key):
        return GrantVerification(False, GRANT_FORGED)
    if grant.grant_id in revoked_ids:
        return GrantVerification(False, GRANT_REVOKED)
    if now < grant.issued_at:
        return GrantVerification(False, GRANT_NOT_YET_VALID)
    if now >= grant.expires_at:
        return GrantVerification(False, GRANT_EXPIRED)
    return GrantVerification(True, GRANT_OK)
