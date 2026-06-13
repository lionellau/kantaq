"""E04-T4 — signed writes end to end through the runtime, and fail-closed.

With the signing cutover on (``sign_events=True``) a tracker write produces an
Ed25519-signed event carrying the member's capability grant as ``policy_ref``,
verifiable against the issuing device's root key — the exact chain the backend
checks in E24-T5. With signing on but no device key, the write fails closed
(503) rather than committing an unsigned event. Off (the default), writes stay
unsigned, byte-for-byte as before the cutover.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService, TokenVerifier, verification_roots
from kantaq_db.models import CapabilityGrantRow, EventLog
from kantaq_protocol import verify
from kantaq_runtime.app import create_app
from kantaq_runtime.auth import ensure_device_identity, keychain_for
from kantaq_runtime.config import Settings
from kantaq_sync_engine import row_to_event
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _settings(tmp_path: Path, *, sign_events: bool) -> Settings:
    return Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"), sign_events=sign_events)


def _app(engine: Engine, settings: Settings) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    return create_app(settings=settings, engine=engine, verifier=verifier)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _bootstrap_owner(engine: Engine) -> str:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted.plaintext


def _ticket_events(engine: Engine) -> list[Any]:
    with Session(engine) as session:
        rows = session.exec(select(EventLog).where(EventLog.collection == "tickets")).all()
        return [row_to_event(row) for row in rows]


def _make_ticket(client: TestClient, token: str) -> None:
    project = client.post("/v1/projects", json={"name": "P"}, headers=_bearer(token))
    assert project.status_code == 201, project.text
    ticket = client.post(
        "/v1/tickets",
        json={"project_id": project.json()["id"], "title": "T"},
        headers=_bearer(token),
    )
    assert ticket.status_code == 201, ticket.text


def test_write_is_signed_under_the_members_grant(engine: Engine, tmp_path: Path) -> None:
    settings = _settings(tmp_path, sign_events=True)
    token = _bootstrap_owner(engine)
    # Provision the runtime device key (the cutover precondition).
    ensure_device_identity(engine, keychain_for(settings))

    with TestClient(_app(engine, settings)) as client:
        _make_ticket(client, token)

    events = _ticket_events(engine)
    assert events, "a ticket event was logged"
    event = events[-1]
    assert event.sig is not None
    assert event.policy_ref is not None

    with Session(engine) as session:
        roots = verification_roots(session)
        grant = session.get(CapabilityGrantRow, event.policy_ref)
    assert grant is not None
    assert grant.subject == event.actor_id  # the grant authorises this actor
    # The signature verifies against the *issuing device's* root key (E24-T5).
    assert verify(event, roots[grant.issuer])


def test_signing_on_without_a_device_key_fails_closed(engine: Engine, tmp_path: Path) -> None:
    settings = _settings(tmp_path, sign_events=True)
    token = _bootstrap_owner(engine)  # no ensure_device_identity → no device key

    with TestClient(_app(engine, settings)) as client:
        response = client.post("/v1/projects", json={"name": "P"}, headers=_bearer(token))
    assert response.status_code == 503


def test_reads_work_without_signing(engine: Engine, tmp_path: Path) -> None:
    """A GET never needs the signer, so signing-on + no device key still reads."""
    settings = _settings(tmp_path, sign_events=True)
    token = _bootstrap_owner(engine)
    with TestClient(_app(engine, settings)) as client:
        response = client.get("/v1/tickets", headers=_bearer(token))
    assert response.status_code == 200


def test_default_runtime_writes_stay_unsigned(engine: Engine, tmp_path: Path) -> None:
    settings = _settings(tmp_path, sign_events=False)  # the default — pre-cutover
    token = _bootstrap_owner(engine)
    with TestClient(_app(engine, settings)) as client:
        _make_ticket(client, token)

    events = _ticket_events(engine)
    assert events
    assert all(event.sig is None for event in events)
    assert all(event.policy_ref is None for event in events)
