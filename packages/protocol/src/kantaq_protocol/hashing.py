"""The audit hash chain (FR-E07-4) — a hash-linked, tamper-evident log.

This is the hashing half of the protocol core (MOD-17): the audit log (MOD-07)
chains its rows with it the way the sync engine signs events with the Ed25519
half. The construction is a **hash-linked log** (Haber-Stornetta time-stamping,
Schneier-Kelsey "Secure Audit Logs" — the precursor to RFC 6962 transparency
logs, whose Merkle anchoring is the E07 v0.2 step). Each link commits to the
prior link's digest **and** the new row's immutable content, so any reorder,
removal, insertion, or content edit of a row below the app-layer append-only
guards (the DEBT-01 raw-SQL path) is *evident* on verification, even though the
guards cannot *refuse* it.

    link[n] = SHA-256( DOMAIN ‖ previous_hex ‖ 0x00 ‖ canonical(record[n]) )

- **One codec.** ``record`` is encoded with the same canonical RFC 8785
  restricted profile (``canonical.canonicalize``) every signature uses, so the
  digest is byte-identical in SQLite and Postgres — never a second, forkable
  encoding (the named E03/E04 risk).
- **Domain separation.** The ``kantaq:audit-chain:v1`` tag means an audit-link
  digest can never collide with an event or grant signing message, exactly as
  ``EVENT_SIGNING_DOMAIN`` / ``GRANT_SIGNING_DOMAIN`` separate those.
- **Unambiguous concatenation.** ``previous`` is either 64 lowercase hex
  characters (a SHA-256 digest) or empty for the genesis row, and a NUL byte —
  which neither hex nor a canonical document can contain at that position —
  separates it from the canonical record, so ``(previous, record)`` has exactly
  one byte spelling.
- **Strict hex.** Like signatures and keys, a digest is 64 lowercase hex
  characters (``HASH_HEX``); ``bytes.fromhex`` leniency (uppercase, whitespace)
  is refused so two spellings can never verify as one link.

SHA-256 (FIPS 180-4) is taken from the stdlib ``hashlib``; no hashing library
clears the golden-rule bar and none is needed (recorded in docs/stack.md).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from kantaq_protocol.canonical import canonicalize
from kantaq_protocol.errors import SchemaViolation

# Domain-separation tag (see canonical.EVENT_SIGNING_DOMAIN / grants.GRANT_SIGNING_DOMAIN).
AUDIT_CHAIN_DOMAIN = b"kantaq:audit-chain:v1\x00"

# A chain digest is a SHA-256 hash rendered as 64 lowercase hex characters —
# the same strictness SIG_HEX / KEY_HEX apply to signatures and keys.
HASH_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


def chain_hash(previous: str | None, record: Mapping[str, Any]) -> str:
    """One link of the audit hash chain (FR-E07-4).

    ``previous`` is the prior row's ``chain_hash`` (64 lowercase hex), or
    ``None`` for the genesis row. ``record`` is the row's immutable content as
    an in-profile JSON object; it is encoded with the one canonical codec, so
    the link is reproducible across stores. Returns the 64-hex digest.

    Raises ``SchemaViolation`` if ``previous`` is not a valid digest spelling or
    ``record`` is outside the canonical profile (e.g. contains a float).
    """
    if previous is not None and (not isinstance(previous, str) or HASH_HEX.match(previous) is None):
        raise SchemaViolation("previous chain hash must be 64 lowercase hex characters or None")
    if not isinstance(record, Mapping):
        raise SchemaViolation("a chain record must be a JSON object")
    digest = hashlib.sha256()
    digest.update(AUDIT_CHAIN_DOMAIN)
    digest.update((previous or "").encode("ascii"))
    digest.update(b"\x00")
    digest.update(canonicalize(dict(record)))
    return digest.hexdigest()
