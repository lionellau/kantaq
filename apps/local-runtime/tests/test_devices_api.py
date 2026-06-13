"""Devices API over HTTP: list the trust roots + decommission (E20-T2, MOD-12).

The read surface is open to any authenticated member (verify keys are public
material); decommission is a credential-management action (``tokens.rotate``).
SEC pins: the device private seed never appears in a response or the schema,
and this runtime's own active device cannot be decommissioned (it would strand
grant issuance until a re-key v0.1 has no flow for).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import (
    DEVICE_KEY_NAME,
    IdentityService,
    Role,
    TokenVerifier,
    ensure_device,
    verification_roots,
)
from kantaq_db.models import AuditEvent, CapabilityGrantRow, Device
from kantaq_runtime.app import create_app
from kantaq_runtime.auth import keychain_for
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))


@pytest.fixture
def app(engine: Engine, settings: Settings) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    return create_app(settings=settings, engine=engine, verifier=verifier)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner(engine: Engine, settings: Settings) -> tuple[str, str]:
    """Bootstraps the Owner AND the runtime device: (member_id, token)."""
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
        assert minted is not None
        ensure_device(session, keychain_for(settings), member_id=minted.member_id)
        session.commit()
    return minted.member_id, minted.plaintext


@pytest.fixture
def member(engine: Engine, owner: tuple[str, str]) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(email="m@example.com", role=Role.member)
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# A second registered device whose key is NOT in this runtime's keychain, so it
# is a legitimate (non-self) decommission target.
_OTHER_KEY = "b" * 64


def _add_other_device(engine: Engine, owner_id: str) -> str:
    with Session(engine) as session:
        device = Device(public_key=_OTHER_KEY, member_id=owner_id, label="other runtime")
        session.add(device)
        session.commit()
        session.refresh(device)
        return device.id


def _add_grant_issued_by(engine: Engine, *, device_id: str, subject: str) -> str:
    with Session(engine) as session:
        row = CapabilityGrantRow(
            subject=subject,
            issuer=device_id,
            resource="workspace/main",
            verbs=["tickets.read"],
            issued_at=0,
            expires_at=3600,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


# ----------------------------------------------------------------------- read


def test_devices_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/devices").status_code == 401
    assert client.post("/v1/devices/x/revoke").status_code == 401


def test_list_shows_the_boot_device_as_current(client: TestClient, owner: tuple[str, str]) -> None:
    _, token = owner
    rows = client.get("/v1/devices", headers=_bearer(token)).json()
    assert len(rows) == 1
    device = rows[0]
    assert device["active"] is True
    assert device["is_current"] is True
    assert device["member_email"] == "owner@local"
    assert len(device["public_key"]) == 64


def test_any_member_can_read_devices(client: TestClient, member: tuple[str, str]) -> None:
    _, token = member
    assert client.get("/v1/devices", headers=_bearer(token)).status_code == 200


def test_list_never_carries_the_device_seed(
    client: TestClient, settings: Settings, owner: tuple[str, str]
) -> None:
    """The seed is keychain-only; the row and every response hold the verify key
    alone (NFR-E06-1 extended to devices, sprint exit criterion 3)."""
    _, token = owner
    seed = keychain_for(settings).get(DEVICE_KEY_NAME)
    assert seed is not None
    listed = client.get("/v1/devices", headers=_bearer(token))
    schema = client.get("/openapi.json")
    for response in (listed, schema):
        assert seed not in response.text


# ---------------------------------------------------------------- decommission


def test_decommission_a_foreign_device_cascades_and_audits(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    owner_id, token = owner
    device_id = _add_other_device(engine, owner_id)
    grant_id = _add_grant_issued_by(engine, device_id=device_id, subject=owner_id)

    response = client.post(f"/v1/devices/{device_id}/revoke", headers=_bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active"] is False
    assert body["revoked_at"] is not None

    with Session(engine) as session:
        # It left the root-of-trust map ...
        assert device_id not in verification_roots(session)
        # ... its issued grant is cascade-revoked ...
        grant = session.get(CapabilityGrantRow, grant_id)
        assert grant is not None and grant.revoked_at is not None
        # ... and both writes are audited.
        actions = [a.action for a in session.exec(select(AuditEvent)).all()]
        assert "device.revoke" in actions
        assert "grant.revoke" in actions


def test_cannot_decommission_own_active_device(
    client: TestClient, engine: Engine, owner: tuple[str, str]
) -> None:
    _, token = owner
    with Session(engine) as session:
        current = session.exec(select(Device)).first()
        assert current is not None
        current_id = current.id
    response = client.post(f"/v1/devices/{current_id}/revoke", headers=_bearer(token))
    assert response.status_code == 409
    assert "own active device" in response.json()["detail"]


def test_decommission_needs_credential_admin(
    client: TestClient, engine: Engine, owner: tuple[str, str], member: tuple[str, str]
) -> None:
    owner_id, _ = owner
    _, member_token = member
    device_id = _add_other_device(engine, owner_id)
    response = client.post(f"/v1/devices/{device_id}/revoke", headers=_bearer(member_token))
    assert response.status_code == 403


def test_decommission_unknown_device_is_404(client: TestClient, owner: tuple[str, str]) -> None:
    _, token = owner
    assert client.post("/v1/devices/nope/revoke", headers=_bearer(token)).status_code == 404
