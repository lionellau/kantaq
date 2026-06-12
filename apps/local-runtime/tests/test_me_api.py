"""The agent-snippet endpoint (E21-T2, MOD-13). SEC pins:

- the response never carries a token (only the placeholder);
- the URL is the member's own loopback gateway, read from the discovery file;
- a stale (dead-pid), malformed, or non-loopback discovery file fails closed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, TokenVerifier
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_runtime.me_api import TOKEN_PLACEHOLDER
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def db_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return data


@pytest.fixture
def app(engine: Engine, db_dir: Path) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    settings = Settings(local_db_path=str(db_dir / "local.sqlite"))
    return create_app(settings=settings, engine=engine, verifier=verifier)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner_token(engine: Engine) -> str:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write_discovery(db_dir: Path, *, url: str, pid: int) -> None:
    (db_dir / "mcp.json").write_text(json.dumps({"url": url, "pid": pid}), encoding="utf-8")


def test_snippet_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/me/agent-snippet").status_code == 401


def test_no_discovery_file_means_gateway_down(client: TestClient, owner_token: str) -> None:
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
    assert body["gateway_url"] is None
    assert body["snippet"] is None
    assert "kantaq mcp dev" in body["instructions"]


def test_live_gateway_yields_the_loopback_snippet(
    client: TestClient, owner_token: str, db_dir: Path
) -> None:
    # This test process stands in for a live gateway: its pid is alive.
    _write_discovery(db_dir, url="http://127.0.0.1:54321/v1/mcp", pid=os.getpid())

    response = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token))
    body = response.json()
    assert body["gateway_live"] is True
    assert body["gateway_url"] == "http://127.0.0.1:54321/v1/mcp"
    server = body["snippet"]["mcpServers"]["kantaq"]
    assert server["url"] == "http://127.0.0.1:54321/v1/mcp"
    assert server["headers"]["Authorization"] == f"Bearer {TOKEN_PLACEHOLDER}"


def test_the_response_never_carries_the_token(
    client: TestClient, owner_token: str, db_dir: Path
) -> None:
    """NFR-E06-1: plaintext appears exactly once, at mint — never here."""
    _write_discovery(db_dir, url="http://127.0.0.1:54321/v1/mcp", pid=os.getpid())
    response = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token))
    assert owner_token not in response.text


def test_a_dead_pid_fails_closed(client: TestClient, owner_token: str, db_dir: Path) -> None:
    # A pid that cannot exist on POSIX (max pid is bounded well below this).
    _write_discovery(db_dir, url="http://127.0.0.1:54321/v1/mcp", pid=2**30)
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
    assert body["snippet"] is None


def test_a_non_loopback_url_fails_closed(
    client: TestClient, owner_token: str, db_dir: Path
) -> None:
    """The snippet may only ever point an agent at the member's own machine."""
    _write_discovery(db_dir, url="http://192.168.1.20:54321/v1/mcp", pid=os.getpid())
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
    assert body["gateway_url"] is None
    assert body["snippet"] is None


def test_malformed_discovery_fails_closed(
    client: TestClient, owner_token: str, db_dir: Path
) -> None:
    (db_dir / "mcp.json").write_text("not json", encoding="utf-8")
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
