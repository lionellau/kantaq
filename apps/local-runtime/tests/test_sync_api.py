"""Sync status API over HTTP (E20-T2, MOD-12).

Read-only and honest: it moves no data (the push/pull engine is Sprint 4), it
reports the configured backend mode and the local event-log state. Pins: local
mode reports no remote backend; the pending/committed counts and last-commit
time reflect the event log; supabase mode with a URL reports configured.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, TokenVerifier
from kantaq_db.models import EventLog
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _make_client(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    app: FastAPI = create_app(settings=settings, engine=engine, verifier=verifier)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Pin the mode explicitly: a dev's real .env may set HUB_MODE=supabase, and
    # this suite must be hermetic (init kwargs override the .env file).
    return Settings(
        local_db_path=str(tmp_path / "data" / "local.sqlite"),
        hub_mode="local",
        supabase_url=None,
    )


@pytest.fixture
def client(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    yield from _make_client(engine, settings)


@pytest.fixture
def owner_token(engine: Engine) -> str:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _add_event(
    engine: Engine, *, seq: int, committed_rev: int | None, created_at: datetime
) -> None:
    with Session(engine) as session:
        session.add(
            EventLog(
                event_id=f"evt{seq:023d}",  # 3 + 23 = 26 chars, unique per seq
                collection="tickets",
                entity_id=f"tkt-{seq}",
                actor_id="actor-1",
                actor_seq=seq,
                op="patch",
                committed_rev=committed_rev,
                created_at=created_at,
            )
        )
        session.commit()


def test_sync_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/sync/status").status_code == 401


def test_local_mode_reports_no_remote_backend(client: TestClient, owner_token: str) -> None:
    body = client.get("/v1/sync/status", headers=_bearer(owner_token)).json()
    assert body["hub_mode"] == "local"
    assert body["backend_configured"] is False
    assert body["pending_events"] == 0
    assert body["committed_events"] == 0
    assert body["total_events"] == 0
    assert body["last_committed_at"] is None


def test_counts_and_last_commit_reflect_the_event_log(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    _add_event(engine, seq=1, committed_rev=1, created_at=datetime(2026, 3, 1, 9, 0, 0))
    _add_event(engine, seq=2, committed_rev=2, created_at=datetime(2026, 3, 2, 9, 0, 0))
    _add_event(engine, seq=3, committed_rev=None, created_at=datetime(2026, 3, 3, 9, 0, 0))

    body = client.get("/v1/sync/status", headers=_bearer(owner_token)).json()
    assert body["total_events"] == 3
    assert body["pending_events"] == 1
    assert body["committed_events"] == 2
    # The latest *committed* event, not the later pending one.
    assert body["last_committed_at"].startswith("2026-03-02")


def test_supabase_mode_reports_configured(engine: Engine, tmp_path: Path) -> None:
    settings = Settings(
        local_db_path=str(tmp_path / "data" / "local.sqlite"),
        hub_mode="supabase",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
    )
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    client_gen = _make_client(engine, settings)
    client = next(client_gen)
    try:
        body = client.get("/v1/sync/status", headers=_bearer(minted.plaintext)).json()
        assert body["hub_mode"] == "supabase"
        assert body["backend_configured"] is True
    finally:
        client_gen.close()
