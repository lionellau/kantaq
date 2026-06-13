"""Bearer tokens for the loopback runtime (FR-E06-1, NFR-E06-1, NFR-E06-2).

Format: ``kq_<token_id>.<secret>`` — the token row's ULID travels inside the
token so verification looks up one row and runs one Argon2id check, instead of
trying every active hash (the GitHub/Stripe keyed-token pattern). Only the
Argon2id PHC hash of the secret is stored (sprint rule: tokens hashed at rest);
the plaintext is shown once at mint and never again.

Argon2id parameters follow RFC 9106 §4 (second recommendation: m=64 MiB, t=3,
p=4). A deliberate-cost hash is too slow to run on every request, so
``TokenVerifier`` keeps a small in-memory cache whose TTL is *under* the 5 s
revocation budget (NFR-E06-2): a revoked token keeps working at most TTL
seconds, and same-process revocations purge the cache immediately. The clock is
injectable so tests pin the TTL with FakeClock.

The production cost is **never weakened**: the only override is an explicit
test-only switch, ``KANTAQ_ARGON2_TEST_FAST=1`` (set by the root ``conftest.py``),
which selects a trivial Argon2id profile so the suite is not dominated by hashing
(DEBT-18). Production never sets the flag, and verification is cost-agnostic — it
reads the parameters out of each PHC string — so a token minted under the fast
profile still verifies, and nothing about the at-rest format changes.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_db.models import Member, Token

TOKEN_PREFIX = "kq_"
_SECRET_BYTES = 32  # 256-bit secrets; URL-safe base64 in the token

# RFC 9106 §4, second recommended option (memory-constrained environments).
# This is the production cost and is never lowered at runtime.
_ARGON2_ITERATIONS = 3
_ARGON2_LANES = 4
_ARGON2_MEMORY_KIB = 64 * 1024
_ARGON2_LENGTH = 32
_ARGON2_SALT_BYTES = 16

# Test-only escape hatch (DEBT-18): when ``KANTAQ_ARGON2_TEST_FAST=1``, hashing
# uses a trivial Argon2id profile so the suite is not dominated by the KDF. This
# is the *minimal valid* profile — cryptography's Argon2id requires
# ``memory_cost >= 8 * lanes`` — and is selected only by this env var, which
# production never sets. Verification ignores it (the cost is encoded in each PHC
# string), so a token minted fast still verifies under any cost.
_ARGON2_TEST_FAST_ENV = "KANTAQ_ARGON2_TEST_FAST"
_ARGON2_FAST_ITERATIONS = 1
_ARGON2_FAST_LANES = 1
_ARGON2_FAST_MEMORY_KIB = 8

# Verified entries live at most this long; must stay < 5 s (NFR-E06-2).
VERIFY_CACHE_TTL_SECONDS = 3.0


def _argon2_cost() -> tuple[int, int, int]:
    """``(iterations, lanes, memory_kib)`` — the production RFC 9106 profile,
    unless a test opts into the trivial profile via ``KANTAQ_ARGON2_TEST_FAST=1``."""
    if os.environ.get(_ARGON2_TEST_FAST_ENV) == "1":
        return _ARGON2_FAST_ITERATIONS, _ARGON2_FAST_LANES, _ARGON2_FAST_MEMORY_KIB
    return _ARGON2_ITERATIONS, _ARGON2_LANES, _ARGON2_MEMORY_KIB


def mint_token(token_id: str) -> tuple[str, str]:
    """Return ``(plaintext, phc_hash)`` for a new bearer token.

    ``plaintext`` is what the member is shown once; ``phc_hash`` is the only
    thing stored. ``token_id`` is the ULID of the token row it belongs to.
    """
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return f"{TOKEN_PREFIX}{token_id}.{secret}", hash_secret(secret)


def parse_token(presented: str) -> tuple[str, str] | None:
    """Split a presented token into ``(token_id, secret)``; None if malformed."""
    if not presented.startswith(TOKEN_PREFIX):
        return None
    body = presented[len(TOKEN_PREFIX) :]
    token_id, sep, secret = body.partition(".")
    if not sep or not token_id or not secret:
        return None
    return token_id, secret


def _kdf() -> Argon2id:
    iterations, lanes, memory_kib = _argon2_cost()
    return Argon2id(
        salt=os.urandom(_ARGON2_SALT_BYTES),
        length=_ARGON2_LENGTH,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_kib,
    )


def hash_secret(secret: str) -> str:
    """Argon2id-hash a token secret into a self-describing PHC string."""
    return _kdf().derive_phc_encoded(secret.encode())


def verify_secret(secret: str, phc_hash: str) -> bool:
    """Constant-cost verify of a secret against its stored PHC hash."""
    try:
        Argon2id.verify_phc_encoded(secret.encode(), phc_hash)
    except (InvalidKey, ValueError):
        return False
    return True


@dataclass(frozen=True)
class VerifiedActor:
    """The result of a successful token verification."""

    member_id: str
    role: str
    token_id: str
    scopes: tuple[str, ...]


@dataclass
class _CacheEntry:
    actor: VerifiedActor
    expires_at: float


class TokenVerifier:
    """Verify presented bearer tokens against the members/tokens tables.

    One instance lives in the runtime process. ``now`` is a monotonic-seconds
    callable (``FakeClock.monotonic`` in tests). The cache holds a SHA-256
    digest of the presented token — never the token itself — so a heap dump
    of the cache yields nothing replayable.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], float] = time.monotonic,
        ttl: float = VERIFY_CACHE_TTL_SECONDS,
    ) -> None:
        if ttl >= 5.0:
            raise ValueError("verify-cache TTL must stay under the 5 s revocation budget")
        self._engine = engine
        self._now = now
        self._ttl = ttl
        self._cache: dict[str, _CacheEntry] = {}

    def verify(self, presented: str) -> VerifiedActor | None:
        """Return the actor for a valid token; None for anything else."""
        digest = hashlib.sha256(presented.encode()).hexdigest()
        entry = self._cache.get(digest)
        if entry is not None and entry.expires_at > self._now():
            return entry.actor
        self._cache.pop(digest, None)

        parsed = parse_token(presented)
        if parsed is None:
            return None
        token_id, secret = parsed

        with Session(self._engine) as session:
            token = session.get(Token, token_id)
            if token is None or token.revoked_at is not None:
                return None
            member = session.get(Member, token.member_id)
            if member is None or member.status == "revoked":
                return None
            if not verify_secret(secret, token.hashed):
                return None
            if member.status == "invited":
                member.status = "active"
                member.updated_at = datetime.now(UTC)
                session.add(member)
                session.commit()
                session.refresh(member)
            actor = VerifiedActor(
                member_id=member.id,
                role=member.role,
                token_id=token.id,
                scopes=tuple(token.scopes),
            )
        self._cache[digest] = _CacheEntry(actor=actor, expires_at=self._now() + self._ttl)
        return actor

    def invalidate_member(self, member_id: str) -> None:
        """Drop cached sessions for a member (same-process revoke is instant)."""
        self._cache = {
            digest: entry
            for digest, entry in self._cache.items()
            if entry.actor.member_id != member_id
        }

    def invalidate_all(self) -> None:
        self._cache.clear()
