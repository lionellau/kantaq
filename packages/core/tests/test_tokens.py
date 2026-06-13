"""Token hashing and verification (Identity profile, MOD-06).

Pins: tokens hashed at rest (PHC Argon2id, salted), forged and tampered
secrets rejected, and — the NFR-E06-2 budget — a revoked token stops
authenticating within 5 seconds even when the verify cache is warm.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import (
    TokenVerifier,
    hash_secret,
    mint_token,
    parse_token,
    verify_secret,
)
from kantaq_core.identity import tokens as tokens_mod
from kantaq_db.models import Member, Token, Workspace
from kantaq_test_harness.clock import FakeClock


def test_mint_token_format_roundtrips() -> None:
    plaintext, hashed = mint_token("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert plaintext.startswith("kq_")
    parsed = parse_token(plaintext)
    assert parsed is not None
    token_id, secret = parsed
    assert token_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert verify_secret(secret, hashed)


def test_hash_is_argon2id_phc_and_salted() -> None:
    one = hash_secret("the-same-secret")
    two = hash_secret("the-same-secret")
    assert one.startswith("$argon2id$")
    assert one != two  # fresh salt every time — no rainbow-table reuse


def test_forged_and_tampered_secrets_fail_verify() -> None:
    hashed = hash_secret("right-secret")
    assert not verify_secret("wrong-secret", hashed)
    assert not verify_secret("right-secret", hashed[:-4] + "AAAA")  # tampered hash
    assert not verify_secret("right-secret", "not-a-phc-string")


# -- Argon2 cost: production stays RFC 9106; tests opt into a trivial profile (DEBT-18) --


def test_production_argon2_profile_is_rfc_9106(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the test flag absent, the cost is exactly the RFC 9106 §4 second profile."""
    monkeypatch.delenv(tokens_mod._ARGON2_TEST_FAST_ENV, raising=False)
    assert tokens_mod._argon2_cost() == (3, 4, 64 * 1024)  # t=3, p=4, m=64 MiB
    # The constants themselves are the production profile (never weakened).
    assert (
        tokens_mod._ARGON2_ITERATIONS,
        tokens_mod._ARGON2_LANES,
        tokens_mod._ARGON2_MEMORY_KIB,
    ) == (3, 4, 65536)


def test_fast_flag_selects_a_minimal_valid_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tokens_mod._ARGON2_TEST_FAST_ENV, "1")
    iterations, lanes, memory_kib = tokens_mod._argon2_cost()
    assert (iterations, lanes, memory_kib) == (1, 1, 8)
    # cryptography's Argon2id requires memory_cost >= 8 * lanes — stay valid.
    assert memory_kib >= 8 * lanes


def test_verify_is_cost_agnostic_across_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token minted at either cost verifies regardless of the active profile —
    the parameters live in the PHC string, so the fast test cost is safe."""
    monkeypatch.delenv(tokens_mod._ARGON2_TEST_FAST_ENV, raising=False)
    prod_hash = hash_secret("s")  # one full-cost hash (proves the prod path runs)
    assert "m=65536,t=3,p=4" in prod_hash

    monkeypatch.setenv(tokens_mod._ARGON2_TEST_FAST_ENV, "1")
    fast_hash = hash_secret("s")
    assert "m=8,t=1,p=1" in fast_hash

    # Verification ignores the active flag — both hashes still verify either way.
    assert verify_secret("s", prod_hash)
    assert verify_secret("s", fast_hash)
    monkeypatch.delenv(tokens_mod._ARGON2_TEST_FAST_ENV, raising=False)
    assert verify_secret("s", prod_hash)
    assert verify_secret("s", fast_hash)


@pytest.mark.parametrize(
    "malformed",
    ["", "kq_", "kq_idonly", "kq_.secretonly", "nope_abc.def", "abc.def"],
)
def test_parse_token_rejects_malformed(malformed: str) -> None:
    assert parse_token(malformed) is None


# -- TokenVerifier against a real (temp) database ---------------------------


def _seed_member_with_token(
    engine: Engine, *, role: str = "Owner", status: str = "active"
) -> tuple[str, str, str]:
    """Create workspace → member → token; return (member_id, token_id, plaintext)."""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        workspace = Workspace(name="ws")
        session.add(workspace)
        session.flush()
        member = Member(workspace_id=workspace.id, email="a@b.c", role=role, status=status)
        session.add(member)
        session.flush()
        token = Token(member_id=member.id, hashed="placeholder", scopes=[])
        session.add(token)
        session.flush()
        plaintext, hashed = mint_token(token.id)
        token.hashed = hashed
        session.add(token)
        session.commit()
        return member.id, token.id, plaintext


def test_verifier_accepts_valid_token(temp_sqlite: Engine) -> None:
    member_id, token_id, plaintext = _seed_member_with_token(temp_sqlite)
    actor = TokenVerifier(temp_sqlite).verify(plaintext)
    assert actor is not None
    assert actor.member_id == member_id
    assert actor.token_id == token_id
    assert actor.role == "Owner"


def test_verifier_rejects_unknown_revoked_and_forged(temp_sqlite: Engine) -> None:
    member_id, token_id, plaintext = _seed_member_with_token(temp_sqlite)
    verifier = TokenVerifier(temp_sqlite)

    assert verifier.verify("kq_01UNKNOWNTOKENID0000000000.secret") is None
    assert verifier.verify("garbage") is None
    assert verifier.verify(plaintext[:-2] + "xx") is None  # forged secret

    with Session(temp_sqlite) as session:
        token = session.get(Token, token_id)
        assert token is not None
        token.revoked_at = datetime.now(UTC)
        session.add(token)
        session.commit()
    assert verifier.verify(plaintext) is None  # cold cache: revoked is rejected


def test_verifier_rejects_revoked_member_even_with_live_token(temp_sqlite: Engine) -> None:
    member_id, _token_id, plaintext = _seed_member_with_token(temp_sqlite)
    with Session(temp_sqlite) as session:
        member = session.get(Member, member_id)
        assert member is not None
        member.status = "revoked"
        session.add(member)
        session.commit()
    assert TokenVerifier(temp_sqlite).verify(plaintext) is None


def test_revoked_session_stops_within_five_seconds(temp_sqlite: Engine) -> None:
    """NFR-E06-2: a warm cache may serve a revoked token for at most TTL < 5 s."""
    _member_id, token_id, plaintext = _seed_member_with_token(temp_sqlite)
    clock = FakeClock()
    verifier = TokenVerifier(temp_sqlite, now=clock.monotonic)

    assert verifier.verify(plaintext) is not None  # warm the cache
    with Session(temp_sqlite) as session:
        token = session.get(Token, token_id)
        assert token is not None
        token.revoked_at = datetime.now(UTC)
        session.add(token)
        session.commit()

    # Inside the TTL the cache may still answer — that staleness is the budget.
    clock.advance(5.0)
    assert verifier.verify(plaintext) is None  # 5 s later it MUST be gone


def test_same_process_revoke_is_immediate_via_invalidate(temp_sqlite: Engine) -> None:
    member_id, token_id, plaintext = _seed_member_with_token(temp_sqlite)
    clock = FakeClock()
    verifier = TokenVerifier(temp_sqlite, now=clock.monotonic)
    assert verifier.verify(plaintext) is not None

    with Session(temp_sqlite) as session:
        token = session.get(Token, token_id)
        assert token is not None
        token.revoked_at = datetime.now(UTC)
        session.add(token)
        session.commit()
    verifier.invalidate_member(member_id)
    assert verifier.verify(plaintext) is None  # no clock advance needed


def test_first_use_flips_invited_member_to_active(temp_sqlite: Engine) -> None:
    member_id, _token_id, plaintext = _seed_member_with_token(temp_sqlite, status="invited")
    assert TokenVerifier(temp_sqlite).verify(plaintext) is not None
    with Session(temp_sqlite) as session:
        member = session.get(Member, member_id)
        assert member is not None
        assert member.status == "active"


def test_ttl_at_or_over_budget_is_rejected(temp_sqlite: Engine) -> None:
    with pytest.raises(ValueError, match="5 s"):
        TokenVerifier(temp_sqlite, ttl=5.0)
