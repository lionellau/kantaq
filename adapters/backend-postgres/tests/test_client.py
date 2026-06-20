"""E25-T1: the runtime's ``SyncServerBackend`` round-trips against the server.

Drives the HTTP client (the ``BackendPort`` the runtime uses when
``HUB_MODE=postgres``) against the REAL ASGI sync-server over an in-process
transport — proving the wire contract end to end: session handshake, commit,
pull, snapshot, and the ack watermark, plus that an invalid token is refused at
the client edge.
"""

from __future__ import annotations

import itertools

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_backend_postgres import SyncBackendError, SyncServerBackend, create_app
from kantaq_core.identity.tokens import mint_token
from kantaq_db.models import Member, Token
from kantaq_protocol import Event
from kantaq_sync_engine.events import SYNC_VERSION

from .conftest import WORKSPACE_ID

MEMBER = "mbr_cli00".ljust(26, "0")
_eid = itertools.count(1)
_seq = itertools.count(1)


SERVER_URL = "http://testserver"


@pytest.fixture
def client_for_server(pg_engine: Engine) -> httpx.Client:
    """A sync httpx client wired to the real ASGI app (Starlette TestClient)."""
    return TestClient(create_app(pg_engine))


@pytest.fixture
def token(pg_engine: Engine) -> str:
    token_id = "tok_cli00".ljust(26, "0")
    plaintext, phc = mint_token(token_id)
    with Session(pg_engine) as session:
        session.add(
            Member(id=MEMBER, workspace_id=WORKSPACE_ID, email="cli@acme.dev", role="Owner")
        )
        session.flush()
        session.add(Token(id=token_id, member_id=MEMBER, hashed=phc))
        session.commit()
    return plaintext


def _event(**payload: object) -> Event:
    return Event(
        event_id=f"e{next(_eid):025d}",
        collection="tickets",
        entity_id="tkt_cli00".ljust(26, "0"),
        actor_id=MEMBER,
        actor_seq=next(_seq),
        op="patch",
        base_rev=None,
        policy_ref=None,
        payload=dict(payload),
        sig=None,
    )


def test_backend_round_trips_through_the_client(
    client_for_server: httpx.Client, token: str
) -> None:
    backend = SyncServerBackend(SERVER_URL, token, client=client_for_server)

    init = backend.session_init(sync_version=SYNC_VERSION, schema_version=1)
    assert init.sync_version == SYNC_VERSION

    out = backend.commit_events([_event(title="A", status="todo")], require_signature=False)
    assert out[0].status == "committed"

    pulled = backend.pull("tickets")
    assert len(pulled) == 1 and pulled[0].event.payload["title"] == "A"

    snap = backend.snapshot("tickets")
    assert snap["tkt_cli00".ljust(26, "0")]["status"] == "todo"

    backend.update_ack_watermark(member_id=MEMBER, replica_id="dev_1", acked_rev=out[0].revision)
    assert backend.safe_watermark_rev() == out[0].revision


def test_invalid_token_surfaces_an_error(client_for_server: httpx.Client) -> None:
    backend = SyncServerBackend(SERVER_URL, "kqt_bogus.nope", client=client_for_server)
    with pytest.raises(SyncBackendError) as exc:
        backend.pull("tickets")
    assert exc.value.status_code == 401
