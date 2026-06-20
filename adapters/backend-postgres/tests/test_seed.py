"""E25-T2: the self-host bootstrap mints a working member token."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from kantaq_backend_postgres.seed import seed_member
from kantaq_core.identity.tokens import TokenVerifier


def test_seed_member_mints_a_token_that_authenticates(pg_engine: Engine) -> None:
    member_id, token = seed_member(pg_engine, email="founder@acme.dev", workspace_name="Acme")
    actor = TokenVerifier(pg_engine).verify(token)
    assert actor is not None
    assert actor.member_id == member_id
    assert actor.role == "Owner"


def test_seed_member_reuses_the_workspace_and_member(pg_engine: Engine) -> None:
    first_id, _ = seed_member(pg_engine, email="founder@acme.dev")
    # a second call for the same email reuses the member (and rotates the token)
    second_id, token2 = seed_member(pg_engine, email="founder@acme.dev")
    assert second_id == first_id
    assert TokenVerifier(pg_engine).verify(token2) is not None
