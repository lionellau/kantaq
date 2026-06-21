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

from kantaq_core.identity import IdentityService, Role, TokenVerifier
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


# ---------------------------------------------------------------- GET /v1/me


def test_me_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/me").status_code == 401


def test_me_returns_the_callers_identity(client: TestClient, owner_token: str) -> None:
    body = client.get("/v1/me", headers=_bearer(owner_token)).json()
    assert body["email"] == "owner@local"
    assert body["role"] == "Owner"
    assert body["workspace_name"] == "Local Workspace"
    assert body["member_id"]
    assert body["workspace_id"]
    # A human token carries no scopes — the role decides.
    assert body["scopes"] == []


def test_me_reflects_an_agent_tokens_scopes(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    with Session(engine) as session:
        minted = IdentityService(session).invite(
            email="bot@example.com",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
    body = client.get("/v1/me", headers=_bearer(minted.plaintext)).json()
    assert body["email"] == "bot@example.com"
    assert body["role"] == "Agent"
    assert sorted(body["scopes"]) == ["proposals.write", "tickets.read"]


def test_me_never_carries_a_secret(client: TestClient, owner_token: str) -> None:
    """The token came in; nothing token-shaped goes back out."""
    response = client.get("/v1/me", headers=_bearer(owner_token))
    assert owner_token not in response.text


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


def test_live_gateway_offers_both_transports_for_every_client(
    client: TestClient, owner_token: str, db_dir: Path
) -> None:
    """E09-T5: a live runtime hands out HTTP *and* stdio configs for Claude Code,
    Cursor, and Codex — six entries, two transports per client."""
    url = "http://127.0.0.1:54321/v1/mcp"
    _write_discovery(db_dir, url=url, pid=os.getpid())

    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    by_key = {(c["client"], c["transport"]): c for c in body["clients"]}
    assert set(by_key) == {
        ("claude_code", "http"),
        ("cursor", "http"),
        ("codex", "http"),
        ("claude_code", "stdio"),
        ("cursor", "stdio"),
        ("codex", "stdio"),
    }

    # Back-compat: the bare ``snippet`` is still the HTTP Claude Code config, and
    # it is the FIRST entry (consumers reading clients[0] keep working).
    http_claude = by_key[("claude_code", "http")]
    assert body["snippet"] == http_claude["config"]
    assert body["clients"][0] == http_claude

    # --- HTTP variants point at the live URL ---
    claude_server = http_claude["config"]["mcpServers"]["kantaq"]
    assert claude_server["type"] == "http"
    assert claude_server["url"] == url
    assert claude_server["headers"]["Authorization"] == f"Bearer {TOKEN_PLACEHOLDER}"

    cursor_server = by_key[("cursor", "http")]["config"]["mcpServers"]["kantaq"]
    assert "type" not in cursor_server  # Cursor takes a bare url for a remote server
    assert cursor_server["url"] == url

    codex_http = by_key[("codex", "http")]
    assert codex_http["config"]["mcp_servers"]["kantaq"]["url"] == url
    assert codex_http["setup"] == f"export KANTAQ_AGENT_TOKEN={TOKEN_PLACEHOLDER}"

    # --- stdio variants launch `kantaq mcp stdio`; the token rides an env var ---
    stdio_claude = by_key[("claude_code", "stdio")]["config"]["mcpServers"]["kantaq"]
    assert stdio_claude["command"] == "kantaq"
    assert stdio_claude["args"] == ["mcp", "stdio"]
    assert stdio_claude["env"]["KANTAQ_MCP_TOKEN"] == TOKEN_PLACEHOLDER
    assert "url" not in stdio_claude  # no HTTP URL on the stdio config

    codex_stdio = by_key[("codex", "stdio")]
    assert codex_stdio["format"] == "toml"
    codex_stdio_server = codex_stdio["config"]["mcp_servers"]["kantaq"]
    assert codex_stdio_server["command"] == "kantaq"
    assert codex_stdio_server["env"]["KANTAQ_MCP_TOKEN"] == TOKEN_PLACEHOLDER
    assert 'command = "kantaq"' in codex_stdio["text"]


def test_gateway_down_still_offers_stdio_clients(client: TestClient, owner_token: str) -> None:
    """E09-T5: stdio needs no live HTTP gateway (the client spawns the gateway
    itself), so the stdio configs are offered even when the gateway is down; only
    the HTTP variants require the discovered URL."""
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
    assert body["snippet"] is None
    assert {c["transport"] for c in body["clients"]} == {"stdio"}
    assert {c["client"] for c in body["clients"]} == {"claude_code", "cursor", "codex"}


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


def test_an_oversized_discovery_file_fails_closed(
    client: TestClient, owner_token: str, db_dir: Path
) -> None:
    """A same-user process dumping a huge mcp.json must not buffer-bomb the
    runtime: anything past the size cap reads as 'no gateway'."""
    blob = '{"url": "http://127.0.0.1:54321/v1/mcp", "pid": 1, "pad": "' + "x" * (128 * 1024) + '"}'
    (db_dir / "mcp.json").write_text(blob, encoding="utf-8")
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
    assert body["snippet"] is None


def test_a_symlinked_discovery_file_fails_closed(
    client: TestClient, owner_token: str, db_dir: Path, tmp_path: Path
) -> None:
    """mcp.json must be a regular file in the data dir — a symlink pointing
    elsewhere is someone else's payload."""
    foreign = tmp_path / "foreign.json"
    foreign.write_text(
        json.dumps({"url": "http://127.0.0.1:54321/v1/mcp", "pid": os.getpid()}),
        encoding="utf-8",
    )
    (db_dir / "mcp.json").symlink_to(foreign)
    body = client.get("/v1/me/agent-snippet", headers=_bearer(owner_token)).json()
    assert body["gateway_live"] is False
    assert body["snippet"] is None
