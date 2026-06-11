"""Token-gated loopback auth (E06-T2, SEC): localhost is not trust.

Pins: a token is required even on 127.0.0.1; cross-origin requests are
rejected before auth; revocation propagates within 5 s even with a warm
verify cache; /healthz and the SPA stay open.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, MintedToken, TokenVerifier
from kantaq_runtime.app import create_app
from kantaq_runtime.auth import RUNTIME_TOKEN_KEY, ensure_local_identity, keychain_for
from kantaq_runtime.config import Settings
from kantaq_test_harness import FakeKeychain
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(engine: Engine, clock: FakeClock) -> FastAPI:
    verifier = TokenVerifier(engine, now=clock.monotonic)
    return create_app(settings=Settings(), engine=engine, verifier=verifier)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner(engine: Engine) -> MintedToken:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted


def _bearer(minted: MintedToken) -> dict[str, str]:
    return {"Authorization": f"Bearer {minted.plaintext}"}


def test_api_requires_token_even_on_localhost(client: TestClient, owner: MintedToken) -> None:
    response = client.get("/v1/members")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_forged_and_malformed_tokens_are_401(client: TestClient, owner: MintedToken) -> None:
    assert client.get("/v1/members", headers={"Authorization": "Bearer junk"}).status_code == 401
    forged = owner.plaintext[:-2] + "xx"
    assert (
        client.get("/v1/members", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    )
    assert (
        client.get("/v1/members", headers={"Authorization": "Basic dXNlcjpwdw=="}).status_code
        == 401
    )


def test_valid_token_passes(client: TestClient, owner: MintedToken) -> None:
    response = client.get("/v1/members", headers=_bearer(owner))
    assert response.status_code == 200
    assert response.json()[0]["role"] == "Owner"


def test_cross_origin_is_rejected_before_auth(client: TestClient, owner: MintedToken) -> None:
    # A web page at evil.example can fire requests at 127.0.0.1:3939; the
    # Origin header gives it away and the runtime refuses (DNS-rebind/CSRF).
    response = client.get(
        "/v1/members", headers={**_bearer(owner), "Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_own_origin_and_no_origin_are_allowed(client: TestClient, owner: MintedToken) -> None:
    own = client.get("/v1/members", headers={**_bearer(owner), "Origin": "http://127.0.0.1:3939"})
    assert own.status_code == 200  # the served SPA
    cli = client.get("/v1/members", headers=_bearer(owner))
    assert cli.status_code == 200  # curl / agents send no Origin


def test_healthz_and_spa_stay_open(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/agents").status_code == 200  # SPA deep-link fallback


def test_revoked_token_stops_within_five_seconds_warm_cache(
    client: TestClient, engine: Engine, clock: FakeClock, owner: MintedToken
) -> None:
    """NFR-E06-2 at the HTTP surface: revoke → requests rejected within 5 s."""
    assert client.get("/v1/members", headers=_bearer(owner)).status_code == 200  # warm
    from datetime import UTC, datetime

    from kantaq_db.models import Token

    with Session(engine) as session:
        token = session.get(Token, owner.token_id)
        assert token is not None
        token.revoked_at = datetime.now(UTC)
        session.add(token)
        session.commit()
    clock.advance(5.0)
    assert client.get("/v1/members", headers=_bearer(owner)).status_code == 401


def test_bootstrap_mints_owner_once_and_parks_token_in_keychain(engine: Engine) -> None:
    keychain = FakeKeychain()
    first = ensure_local_identity(engine, keychain)
    assert first is not None
    assert keychain.get(RUNTIME_TOKEN_KEY) == first
    assert TokenVerifier(engine).verify(first) is not None
    assert ensure_local_identity(engine, keychain) is None  # second boot: no-op
    assert keychain.get(RUNTIME_TOKEN_KEY) == first  # token not churned


def test_keychain_for_lives_next_to_the_database(tmp_path: Path) -> None:
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))
    keychain = keychain_for(settings)
    keychain.set(RUNTIME_TOKEN_KEY, "kq_x.y")
    assert (tmp_path / "data" / "keychain" / RUNTIME_TOKEN_KEY).is_file()
