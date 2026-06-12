"""Offline capability-grant verification (FR-E03-4, E03-T3).

A grant verifies against a set of **roots** — the registered device verify
keys (MOD-06's ``devices`` table) — with no network and no clock authority
beyond the caller's ``now``: a teammate's runtime can check a grant on a
plane. Verification answers exactly one question — *was this permission slip
issued by a known device and is it currently valid* — and returns a
structured result, never a bare bool, so callers and audit rows can say why
a grant was refused.

Revocation state (a ``revoked_at`` on the stored row) is the identity
store's knowledge, deliberately not encoded here: a signature cannot prove
an absence. MOD-06 layers the store check on top of this offline check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from kantaq_protocol.canonical import canonicalize
from kantaq_protocol.entities import CapabilityGrant
from kantaq_protocol.errors import SchemaViolation
from kantaq_protocol.signing import sign_bytes, verify_bytes

# Structured refusal reasons (wire vocabulary, like the FR-E03-5 codes).
GRANT_OK = "ok"
GRANT_MISSING_SIGNATURE = "missing_signature"
GRANT_FORGED = "forged"
GRANT_UNKNOWN_ROOT = "unknown_root"
GRANT_EXPIRED = "expired"
GRANT_NOT_YET_VALID = "not_yet_valid"
GRANT_INVALID_VALIDITY = "invalid_validity"


@dataclass(frozen=True, slots=True)
class GrantVerification:
    """The outcome of one offline check: ``ok`` or a named refusal."""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def _grant_mapping(grant: CapabilityGrant, *, include_sig: bool) -> dict[str, Any]:
    for name in ("grant_id", "subject", "issuer", "resource"):
        if not getattr(grant, name):
            raise SchemaViolation(f"grant field {name!r} must be non-empty")
    if not grant.verbs:
        raise SchemaViolation("a grant must carry at least one verb")
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
    """What the issuer's device signs: the canonical grant minus ``sig``."""
    return canonicalize(_grant_mapping(grant, include_sig=False))


def encode_canonical_grant(grant: CapabilityGrant) -> bytes:
    """The grant's full canonical wire form (includes ``sig`` when present)."""
    return canonicalize(_grant_mapping(grant, include_sig=True))


def sign_grant(grant: CapabilityGrant, private_key_hex: str) -> CapabilityGrant:
    """A copy of the grant signed by the issuing device's key."""
    return replace(grant, sig=sign_bytes(grant_signing_bytes(grant), private_key_hex))


def verify_grant(
    grant: CapabilityGrant,
    roots: Mapping[str, str],
    *,
    now: int,
) -> GrantVerification:
    """Offline check: signature against a known root, validity against ``now``.

    ``roots`` maps issuer (device) id -> hex Ed25519 verify key. ``now`` is
    unix seconds UTC. Order matters for honest audit reasons: a forged
    signature is reported as forged even when the grant is also expired.
    """
    if grant.expires_at <= grant.issued_at:
        return GrantVerification(False, GRANT_INVALID_VALIDITY)
    if grant.sig is None:
        return GrantVerification(False, GRANT_MISSING_SIGNATURE)
    root_key = roots.get(grant.issuer)
    if root_key is None:
        return GrantVerification(False, GRANT_UNKNOWN_ROOT)
    if not verify_bytes(grant_signing_bytes(grant), grant.sig, root_key):
        return GrantVerification(False, GRANT_FORGED)
    if now < grant.issued_at:
        return GrantVerification(False, GRANT_NOT_YET_VALID)
    if now >= grant.expires_at:
        return GrantVerification(False, GRANT_EXPIRED)
    return GrantVerification(True, GRANT_OK)
