"""The RLS test doubles test themselves (MOD-30 rule) — hermetic half.

The live half (the stub + TamperedClient against a real Postgres) is exercised
by the Supabase backend suite (``adapters/backend-supabase/tests/test_rls.py``),
which is also the contract test: the stub's ``auth.*`` definitions must make
the checked-in Supabase policies behave as they do in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from kantaq_test_harness.rls import SUPABASE_ROLES, Attempt, TamperedClient, supabase_claims


def test_claims_are_shaped_like_a_supabase_access_token() -> None:
    claims = supabase_claims("alice@acme.dev")
    assert claims["email"] == "alice@acme.dev"
    assert claims["role"] == "authenticated"
    assert claims["sub"]  # a uuid-shaped subject is always present


def test_claims_accept_overrides_and_extras() -> None:
    claims = supabase_claims("e@x.dev", role="service_role", aud="authenticated")
    assert claims["role"] == "service_role"
    assert claims["aud"] == "authenticated"


def test_tampered_client_only_impersonates_supabase_roles() -> None:
    """SET ROLE is interpolated; the allowlist is the injection guard."""
    engine = create_engine("sqlite://")
    for role in SUPABASE_ROLES:
        TamperedClient(engine, claims={}, role=role)
    with pytest.raises(ValueError, match="role must be one of"):
        TamperedClient(engine, claims={}, role='postgres" ; drop table tickets; --')


def test_attempt_denial_covers_both_rls_shapes() -> None:
    # WITH CHECK violations raise; USING filters silently match nothing.
    assert Attempt(ok=False, error="new row violates row-level security").denied
    assert Attempt(ok=True, rowcount=0).denied
    assert not Attempt(ok=True, rowcount=1).denied
