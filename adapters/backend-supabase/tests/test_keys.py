"""The anon-key guard: a service-role key never arms a client (NFR-E24-1)."""

from __future__ import annotations

import jwt
import pytest

from kantaq_backend_supabase.keys import ServiceRoleKeyError, assert_client_safe_key, key_role

_SECRET = "test-only-signing-secret-0123456789abcdef"
ANON_KEY = jwt.encode({"role": "anon", "iss": "supabase"}, _SECRET, algorithm="HS256")
SERVICE_KEY = jwt.encode({"role": "service_role", "iss": "supabase"}, _SECRET, algorithm="HS256")


def test_key_role_reads_the_role_claim() -> None:
    assert key_role(ANON_KEY) == "anon"
    assert key_role(SERVICE_KEY) == "service_role"


def test_key_role_is_none_for_non_jwt_keys() -> None:
    assert key_role("sb_publishable_abc123") is None
    assert key_role("") is None


def test_anon_key_passes_the_guard() -> None:
    assert assert_client_safe_key(ANON_KEY) == ANON_KEY


def test_opaque_key_passes_the_guard() -> None:
    # Supabase's newer publishable keys are not JWTs; the backend still
    # validates them — the guard only refuses a *positively identified* service key.
    assert assert_client_safe_key("sb_publishable_abc123") == "sb_publishable_abc123"


def test_service_role_key_is_refused() -> None:
    with pytest.raises(ServiceRoleKeyError):
        assert_client_safe_key(SERVICE_KEY)


def test_refusal_never_echoes_the_key() -> None:
    with pytest.raises(ServiceRoleKeyError) as excinfo:
        assert_client_safe_key(SERVICE_KEY)
    assert SERVICE_KEY not in str(excinfo.value)
