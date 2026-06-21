"""Connection-verify tests (E22-T2): hermetic — no real network or backend."""

from pathlib import Path

import httpx

from kantaq_runtime.config import Settings
from kantaq_runtime.verify import verify_connection


def test_local_ok_creates_and_checks_data_dir(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        hub_mode="local",
        local_db_path=str(tmp_path / "db" / "local.sqlite"),
    )
    result = verify_connection(settings)
    assert result.ok
    assert (tmp_path / "db").is_dir()


def test_supabase_missing_credentials_fails_fast() -> None:
    settings = Settings(_env_file=None, hub_mode="supabase")
    result = verify_connection(settings)
    assert not result.ok
    assert "required" in result.message


def test_supabase_reachable_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "anon-key"
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        hub_mode="supabase",
        supabase_url="https://abc.supabase.co",
        supabase_anon_key="anon-key",
    )
    result = verify_connection(settings, client=client)
    assert result.ok


def test_supabase_unhealthy_fails() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _req: httpx.Response(503)))
    settings = Settings(
        _env_file=None,
        hub_mode="supabase",
        supabase_url="https://abc.supabase.co",
        supabase_anon_key="k",
    )
    result = verify_connection(settings, client=client)
    assert not result.ok


def test_supabase_unreachable_fails() -> None:
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    settings = Settings(
        _env_file=None,
        hub_mode="supabase",
        supabase_url="https://abc.supabase.co",
        supabase_anon_key="k",
    )
    result = verify_connection(settings, client=client)
    assert not result.ok
    assert "cannot reach" in result.message


def test_postgres_missing_credentials_fails_fast() -> None:
    settings = Settings(_env_file=None, hub_mode="postgres")
    result = verify_connection(settings)
    assert not result.ok
    assert "HUB_URL and HUB_TOKEN are required" in result.message


def test_postgres_reachable_ok() -> None:
    """Self-host `kantaq dev` boots in postgres mode (regression — verify used to
    reject postgres with 'not supported until v0.3')."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        hub_mode="postgres",
        hub_url="http://hub:8889",
        hub_token="kq_token",
    )
    result = verify_connection(settings, client=client)
    assert result.ok and "reachable" in result.message


def test_postgres_unreachable_fails() -> None:
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    settings = Settings(
        _env_file=None, hub_mode="postgres", hub_url="http://hub:8889", hub_token="kq_token"
    )
    result = verify_connection(settings, client=client)
    assert not result.ok
    assert "cannot reach" in result.message
