"""The signed ``twp://invite`` onboarding bundle (DEBT-04, E06-T8, FR-E06-6).

The protocol-correct join, the twin of ``grants.py``: an ``Invite`` is a
device-signed permission slip to *become a member*, verified offline against the
issuer's device **root**. A maintainer's runtime crafts + signs it; the
invitee's runtime decodes the ``twp://invite/<payload>`` URI, verifies the
signature against the issuer device's known verify key, checks the validity
window, and on accept admits the member with the carried role + grant scope.

Same hardening as grants (E27 gate): strict runtime schema before any
sign/verify, a strict canonical decode (one byte spelling only), domain
separation (``kantaq:invite:v1`` — an invite signature can never validate as a
grant or event signature), and structured refusal reasons (never a bare bool).
The crypto reuses the one canonical codec + ``sign_bytes``/``verify_bytes`` — no
new signature primitive.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from kantaq_protocol.canonical import SIG_HEX, canonicalize, parse_canonical_document
from kantaq_protocol.entities import Invite
from kantaq_protocol.errors import SchemaViolation
from kantaq_protocol.signing import sign_bytes, verify_bytes

# Domain-separation tag (see canonical.EVENT_SIGNING_DOMAIN / grants.GRANT_SIGNING_DOMAIN).
INVITE_SIGNING_DOMAIN = b"kantaq:invite:v1\x00"

# The URI scheme an invite is shared as; the payload is the base64url (no pad)
# of the canonical signed invite bytes.
INVITE_URI_PREFIX = "twp://invite/"

# Structured refusal reasons (wire vocabulary, like the FR-E03-5 / grant codes).
INVITE_OK = "ok"
INVITE_MISSING_SIGNATURE = "missing_signature"
INVITE_FORGED = "forged"
INVITE_UNKNOWN_ROOT = "unknown_root"
INVITE_EXPIRED = "expired"
INVITE_NOT_YET_VALID = "not_yet_valid"
INVITE_INVALID_VALIDITY = "invalid_validity"

_INVITE_REQUIRED = (
    "invite_id",
    "workspace_id",
    "subject_email",
    "role",
    "resource",
    "verbs",
    "issuer",
    "issued_at",
    "expires_at",
)
_INVITE_FIELDS = (*_INVITE_REQUIRED, "sig")


@dataclass(frozen=True, slots=True)
class InviteVerification:
    """The outcome of one offline invite check: ``ok`` or a named refusal."""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_invite(invite: Invite) -> None:
    """Strict runtime schema, shared by encode, sign, verify, and decode."""
    for name in ("invite_id", "workspace_id", "subject_email", "role", "resource", "issuer"):
        value = getattr(invite, name)
        if not isinstance(value, str) or not value:
            raise SchemaViolation(f"invite field {name!r} must be a non-empty string")
    verbs = invite.verbs
    if isinstance(verbs, str | bytes) or not isinstance(verbs, list | tuple) or not verbs:
        raise SchemaViolation("invite field 'verbs' must be a non-empty list of strings")
    for verb in verbs:
        if not isinstance(verb, str) or not verb:
            raise SchemaViolation("invite field 'verbs' must contain only non-empty strings")
    for name in ("issued_at", "expires_at"):
        if not _is_int(getattr(invite, name)):
            raise SchemaViolation(f"invite field {name!r} must be an integer (unix seconds)")
    if invite.sig is not None and (
        not isinstance(invite.sig, str) or SIG_HEX.match(invite.sig) is None
    ):
        raise SchemaViolation("invite field 'sig' must be 128 lowercase hex characters")


def _invite_mapping(invite: Invite, *, include_sig: bool) -> dict[str, Any]:
    _validate_invite(invite)
    mapping: dict[str, Any] = {
        "invite_id": invite.invite_id,
        "workspace_id": invite.workspace_id,
        "subject_email": invite.subject_email,
        "role": invite.role,
        "resource": invite.resource,
        "verbs": list(invite.verbs),
        "issuer": invite.issuer,
        "issued_at": invite.issued_at,
        "expires_at": invite.expires_at,
    }
    if include_sig and invite.sig is not None:
        mapping["sig"] = invite.sig
    return mapping


def invite_signing_bytes(invite: Invite) -> bytes:
    """What the issuer's device signs: the domain tag + canonical invite minus ``sig``."""
    return INVITE_SIGNING_DOMAIN + canonicalize(_invite_mapping(invite, include_sig=False))


def encode_canonical_invite(invite: Invite) -> bytes:
    """The invite's full canonical wire form (includes ``sig`` when present)."""
    return canonicalize(_invite_mapping(invite, include_sig=True))


def decode_invite(data: bytes) -> Invite:
    """Parse canonical bytes back into an ``Invite`` (strict, fail closed)."""
    raw = parse_canonical_document(data)
    unknown = set(raw) - set(_INVITE_FIELDS)
    if unknown:
        raise SchemaViolation(f"unknown invite fields: {sorted(unknown)}")
    missing = set(_INVITE_REQUIRED) - set(raw)
    if missing:
        raise SchemaViolation(f"missing invite fields: {sorted(missing)}")
    if not isinstance(raw["verbs"], list):
        raise SchemaViolation("invite field 'verbs' must be a list")
    invite = Invite(
        invite_id=raw["invite_id"],
        workspace_id=raw["workspace_id"],
        subject_email=raw["subject_email"],
        role=raw["role"],
        resource=raw["resource"],
        verbs=tuple(raw["verbs"]),
        issuer=raw["issuer"],
        issued_at=raw["issued_at"],
        expires_at=raw["expires_at"],
        sig=raw.get("sig"),
    )
    if encode_canonical_invite(invite) != data:
        raise SchemaViolation(
            "input is not in canonical form (re-encoding differs); "
            "only canonical bytes are accepted"
        )
    return invite


def sign_invite(invite: Invite, private_key_hex: str) -> Invite:
    """A copy of the invite signed by the issuing device's key."""
    return replace(invite, sig=sign_bytes(invite_signing_bytes(invite), private_key_hex))


def encode_invite_uri(invite: Invite) -> str:
    """The shareable ``twp://invite/<payload>`` URI for a signed invite."""
    if invite.sig is None:
        raise SchemaViolation("cannot encode an unsigned invite to a URI; sign it first")
    payload = base64.urlsafe_b64encode(encode_canonical_invite(invite)).rstrip(b"=").decode("ascii")
    return INVITE_URI_PREFIX + payload


def decode_invite_uri(uri: str) -> Invite:
    """Parse a ``twp://invite/<payload>`` URI back into an ``Invite`` (strict)."""
    if not isinstance(uri, str) or not uri.startswith(INVITE_URI_PREFIX):
        raise SchemaViolation(f"not a {INVITE_URI_PREFIX!r} invite URI")
    payload = uri[len(INVITE_URI_PREFIX) :]
    if not payload:
        raise SchemaViolation("empty invite payload")
    pad = "=" * (-len(payload) % 4)
    try:
        data = base64.urlsafe_b64decode(payload + pad)
    except (ValueError, TypeError) as exc:
        raise SchemaViolation(f"invite payload is not valid base64url: {exc}") from exc
    return decode_invite(data)


def verify_invite(
    invite: Invite,
    roots: Mapping[str, str],
    *,
    now: int,
) -> InviteVerification:
    """Offline check: signature against a known device root, validity against ``now``.

    ``roots`` maps issuer (device) id -> hex Ed25519 verify key (the invitee's
    ``verification_roots``); ``now`` is unix seconds UTC. Like ``verify_grant``,
    order is chosen for honest audit reasons: a forged signature reports forged
    even when the invite is also expired. An unknown issuer device -> the invite
    was not signed by a root this workspace trusts.
    """
    _validate_invite(invite)
    if invite.expires_at <= invite.issued_at:
        return InviteVerification(False, INVITE_INVALID_VALIDITY)
    if invite.sig is None:
        return InviteVerification(False, INVITE_MISSING_SIGNATURE)
    root_key = roots.get(invite.issuer)
    if root_key is None:
        return InviteVerification(False, INVITE_UNKNOWN_ROOT)
    if not verify_bytes(invite_signing_bytes(invite), invite.sig, root_key):
        return InviteVerification(False, INVITE_FORGED)
    if now < invite.issued_at:
        return InviteVerification(False, INVITE_NOT_YET_VALID)
    if now >= invite.expires_at:
        return InviteVerification(False, INVITE_EXPIRED)
    return InviteVerification(True, INVITE_OK)
