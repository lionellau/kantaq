"""kantaq protocol core: entities, canonical codec, Ed25519, grant verify (MOD-17).

The load-bearing core (Epic E03): every signature in the system is computed
over this module's canonical bytes — one codec, shared by the sync engine
(MOD-04, Sprint 4) and the backends, or signatures break. Golden vectors
live in ``packages/test_harness/fixtures`` and are cross-verified against a
second Ed25519 implementation (D-11).
"""

from __future__ import annotations

from kantaq_protocol import crdt
from kantaq_protocol.canonical import (
    EVENT_SIGNING_DOMAIN,
    MAX_DEPTH,
    MAX_DOCUMENT_BYTES,
    MAX_SAFE_INT,
    canonicalize,
    decode,
    dedup_key,
    encode_canonical,
    signing_bytes,
)
from kantaq_protocol.entities import (
    OPS,
    Actor,
    ActorKind,
    AuditAnchor,
    BlobRef,
    CapabilityGrant,
    Collection,
    Event,
    MergePolicy,
    Op,
    Snapshot,
    TeamManifest,
)
from kantaq_protocol.errors import (
    PolicyDenied,
    ProtocolError,
    SchemaViolation,
    StaleBaseRev,
    UnknownCollection,
)
from kantaq_protocol.grants import (
    GRANT_EXPIRED,
    GRANT_FORGED,
    GRANT_INVALID_VALIDITY,
    GRANT_MISSING_SIGNATURE,
    GRANT_NOT_YET_VALID,
    GRANT_OK,
    GRANT_REVOKED,
    GRANT_SIGNING_DOMAIN,
    GRANT_UNKNOWN_ROOT,
    GrantVerification,
    decode_grant,
    encode_canonical_grant,
    grant_signing_bytes,
    sign_grant,
    verify_grant,
)
from kantaq_protocol.hashing import (
    AUDIT_CHAIN_DOMAIN,
    HASH_HEX,
    chain_hash,
)
from kantaq_protocol.signing import (
    KeyPair,
    generate_keypair,
    public_key_of,
    sign,
    sign_bytes,
    verify,
    verify_bytes,
)

__version__: str = "0.1.0"

__all__ = [
    "AUDIT_CHAIN_DOMAIN",
    "EVENT_SIGNING_DOMAIN",
    "GRANT_EXPIRED",
    "GRANT_FORGED",
    "GRANT_INVALID_VALIDITY",
    "GRANT_MISSING_SIGNATURE",
    "GRANT_NOT_YET_VALID",
    "GRANT_OK",
    "GRANT_REVOKED",
    "GRANT_SIGNING_DOMAIN",
    "GRANT_UNKNOWN_ROOT",
    "HASH_HEX",
    "MAX_DEPTH",
    "MAX_DOCUMENT_BYTES",
    "MAX_SAFE_INT",
    "OPS",
    "Actor",
    "ActorKind",
    "AuditAnchor",
    "BlobRef",
    "CapabilityGrant",
    "Collection",
    "Event",
    "GrantVerification",
    "KeyPair",
    "MergePolicy",
    "Op",
    "PolicyDenied",
    "ProtocolError",
    "SchemaViolation",
    "Snapshot",
    "StaleBaseRev",
    "TeamManifest",
    "UnknownCollection",
    "__version__",
    "canonicalize",
    "chain_hash",
    "crdt",
    "decode",
    "decode_grant",
    "dedup_key",
    "encode_canonical",
    "encode_canonical_grant",
    "generate_keypair",
    "grant_signing_bytes",
    "public_key_of",
    "sign",
    "sign_bytes",
    "sign_grant",
    "signing_bytes",
    "verify",
    "verify_bytes",
    "verify_grant",
]
