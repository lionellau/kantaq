"""E04 wiring: every tracker API write lands one event in the local log.

The MOD-03 rule "all writes go through the sync engine as Events" is closed at
the HTTP surface: entity row, audit row, and event-log row are one transaction,
attributed to the authenticated member.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService, MintedToken, TokenVerifier
from kantaq_db import EventLog
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def app(engine: Engine) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
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


def test_api_writes_append_to_the_event_log(
    client: TestClient, engine: Engine, owner: MintedToken
) -> None:
    headers = {"Authorization": f"Bearer {owner.plaintext}"}
    project = client.post("/v1/projects", json={"name": "P"}, headers=headers).json()
    ticket = client.post(
        "/v1/tickets", json={"project_id": project["id"], "title": "T"}, headers=headers
    ).json()
    patched = client.patch(f"/v1/tickets/{ticket['id']}", json={"status": "doing"}, headers=headers)
    assert patched.status_code == 200

    with Session(engine) as session:
        rows = sorted(session.exec(select(EventLog)).all(), key=lambda r: r.actor_seq)

    assert [(r.collection, r.op) for r in rows] == [
        ("projects", "patch"),
        ("tickets", "patch"),
        ("tickets", "patch"),
    ]
    assert {r.actor_id for r in rows} == {owner.member_id}
    assert all(r.committed_rev is None for r in rows)  # local until pushed (E24-T4)
    assert rows[2].payload["status"] == "doing"
