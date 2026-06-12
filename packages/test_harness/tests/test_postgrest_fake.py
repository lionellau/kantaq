"""FakePostgREST self-tests (MOD-30).

The hermetic half: request parsing, auth handling, and identifier hygiene —
none of it needs a database (the fake fails before touching the engine).
The live contract test is the MOD-05 sync suite, which drives the real
adapter through this fake against Postgres + the checked-in artifacts.
"""

from __future__ import annotations

import httpx
import pytest

from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.postgrest import FakePostgREST, decode_jwt_claims, encode_test_jwt
from kantaq_test_harness.rls import supabase_claims


class _NoEngine:
    """A sentinel engine: any attribute access means the fake leaked SQL."""

    def __getattr__(self, name: str) -> None:
        raise AssertionError(f"the fake touched the engine ({name}) before auth/validation")


def _client(fake: FakePostgREST) -> httpx.Client:
    return fake.client()


def test_jwt_claims_round_trip() -> None:
    claims = supabase_claims("dev@team.dev", role="authenticated")
    assert decode_jwt_claims(encode_test_jwt(claims)) == claims
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims("a.b") == {}


def test_a_missing_apikey_is_401() -> None:
    fake = FakePostgREST(_NoEngine())  # type: ignore[arg-type]
    response = _client(fake).get("/rest/v1/sync_events")
    assert response.status_code == 401
    assert "API key" in response.json()["message"]


def test_an_expired_jwt_is_401_with_a_fake_clock() -> None:
    clock = FakeClock()
    epoch = clock.now().timestamp()
    fake = FakePostgREST(_NoEngine(), now=lambda: clock.now().timestamp())  # type: ignore[arg-type]
    token = encode_test_jwt(supabase_claims("dev@team.dev", exp=epoch - 1))
    response = _client(fake).get(
        "/rest/v1/sync_events",
        headers={"apikey": "k", "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert response.json()["message"] == "JWT expired"


def test_unknown_routes_and_methods_are_refused() -> None:
    fake = FakePostgREST(_NoEngine())  # type: ignore[arg-type]
    assert _client(fake).get("/rest/v1/a/b", headers={"apikey": "k"}).status_code == 404
    assert _client(fake).put("/rest/v1/t", headers={"apikey": "k"}).status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/rest/v1/bad-table;drop",
        "/rest/v1/UPPER",
    ],
)
def test_evil_table_identifiers_are_rejected(path: str) -> None:
    fake = FakePostgREST(_NoEngine())  # type: ignore[arg-type]
    response = _client(fake).get(path, headers={"apikey": "k"})
    assert response.status_code == 400
    assert "identifier" in response.json()["message"]


def test_evil_filter_columns_are_rejected() -> None:
    fake = FakePostgREST(_NoEngine())  # type: ignore[arg-type]
    response = _client(fake).get(
        "/rest/v1/sync_events",
        params={"revision;drop table x": "gt.0"},
        headers={"apikey": "k"},
    )
    assert response.status_code == 400


def test_unsupported_filter_operators_are_rejected() -> None:
    fake = FakePostgREST(_NoEngine())  # type: ignore[arg-type]
    response = _client(fake).get(
        "/rest/v1/sync_events",
        params={"payload": "ilike.%x%"},
        headers={"apikey": "k"},
    )
    assert response.status_code == 400
    assert "not supported" in response.json()["message"]
