"""Skills API over HTTP (E17-T5): container list + mapping CRUD + the role matrix.

Pins the REST face of the db-backed skill registry that the Settings mapping
editor drives: every human may read the containers + mappings (``skills.read``),
Member and up manage mappings (``skills.manage``), a Viewer is 403 on a write,
and no token is 401. The registry is off the sync surface, so a mapping write
emits NO event-log row (re-proven here).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_db.models import EventLog, SkillContainerRow
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def app(engine: Engine, tmp_path: Path) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))
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


@pytest.fixture
def viewer_token(engine: Engine, owner_token: str) -> str:
    with Session(engine) as session:
        return (
            IdentityService(session).invite(email="viewer@example.com", role=Role.viewer).plaintext
        )


@pytest.fixture
def container_id(engine: Engine) -> str:
    """One seeded container — the migration seeds 29, but the tests build the
    schema directly, so we add the one the mapping CRUD points at."""
    with Session(engine) as session:
        row = SkillContainerRow(
            slug="code-review",
            name="Code review",
            recommended_roles=["code_agent"],
            supported_stages=["implementation"],
            required_input="",
            expected_output="findings",
            allowed_tools=[],
            default_write_mode="propose",
            risk_level="medium",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_mapping_crud_round_trip(client: TestClient, owner_token: str, container_id: str) -> None:
    # the container shows up for the picker
    containers = client.get("/v1/skill-containers", headers=_bearer(owner_token))
    assert containers.status_code == 200
    assert any(c["id"] == container_id for c in containers.json())

    created = client.post(
        "/v1/skill-mappings",
        json={"container_id": container_id, "connection": "My Claude Code"},
        headers=_bearer(owner_token),
    )
    assert created.status_code == 201, created.text
    mapping = created.json()
    assert mapping["scope"] == "personal" and mapping["connection"] == "My Claude Code"

    listed = client.get("/v1/skill-mappings", headers=_bearer(owner_token))
    assert [m["id"] for m in listed.json()] == [mapping["id"]]

    patched = client.patch(
        f"/v1/skill-mappings/{mapping['id']}",
        json={"status": "disabled"},
        headers=_bearer(owner_token),
    )
    assert patched.status_code == 200 and patched.json()["status"] == "disabled"

    deleted = client.delete(f"/v1/skill-mappings/{mapping['id']}", headers=_bearer(owner_token))
    assert deleted.status_code == 204
    assert client.get("/v1/skill-mappings", headers=_bearer(owner_token)).json() == []


def test_viewer_reads_but_cannot_manage(
    client: TestClient, viewer_token: str, container_id: str
) -> None:
    # a viewer holds skills.read
    assert client.get("/v1/skill-containers", headers=_bearer(viewer_token)).status_code == 200
    # but not skills.manage
    denied = client.post(
        "/v1/skill-mappings",
        json={"container_id": container_id},
        headers=_bearer(viewer_token),
    )
    assert denied.status_code == 403


def test_no_token_is_unauthorised(client: TestClient) -> None:
    assert client.get("/v1/skill-containers").status_code == 401


def test_unknown_container_is_422(client: TestClient, owner_token: str) -> None:
    bad = client.post(
        "/v1/skill-mappings",
        json={"container_id": "no_such_container_00000000"},
        headers=_bearer(owner_token),
    )
    assert bad.status_code == 422


def test_a_mapping_write_emits_no_event(
    client: TestClient, owner_token: str, container_id: str, engine: Engine
) -> None:
    """The registry is off the sync surface — a mapping write is local + audited,
    never an event-log row (architecture §6.1)."""
    client.post(
        "/v1/skill-mappings",
        json={"container_id": container_id, "connection": "tool"},
        headers=_bearer(owner_token),
    )
    with Session(engine) as session:
        assert session.exec(select(EventLog)).all() == []
