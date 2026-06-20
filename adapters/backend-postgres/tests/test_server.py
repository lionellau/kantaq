"""E25-T1 [SEC]: the sync-server HTTP surface — auth, round-trip, caller-binding.

The server is the networked face of the backend, so its own walls are tested
here: every ``/v1`` route demands a Bearer member token (401 otherwise), the
acting member is bound to the token (an event whose ``actor_id`` is not the
authenticated member is denied — the ``is_self_in_workspace`` wall), and a real
commit → pull → snapshot round-trips over HTTP. The deep grant/merge guarantees
are the backend's (``test_deny`` / ``test_parity``); this pins the transport +
the auth boundary on top of them.
"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_backend_postgres import create_app
from kantaq_core.identity.tokens import mint_token
from kantaq_db.models import Member, Token

from .conftest import WORKSPACE_ID

MEMBER = "mbr_srv00".ljust(26, "0")
_eid = itertools.count(1)
_seq = itertools.count(1)


@pytest.fixture
def token(pg_engine: Engine) -> str:
    """Seed a member + token; return the Bearer plaintext."""
    token_id = "tok_srv00".ljust(26, "0")
    plaintext, phc = mint_token(token_id)
    with Session(pg_engine) as session:
        session.add(
            Member(id=MEMBER, workspace_id=WORKSPACE_ID, email="srv@acme.dev", role="Owner")
        )
        session.flush()
        session.add(Token(id=token_id, member_id=MEMBER, hashed=phc))
        session.commit()
    return plaintext


@pytest.fixture
def client(pg_engine: Engine) -> TestClient:
    return TestClient(create_app(pg_engine))


def _wire(actor_id: str, **payload: object) -> dict[str, object]:
    return {
        "event_id": f"e{next(_eid):025d}",
        "collection": "tickets",
        "entity_id": "tkt_srv00".ljust(26, "0"),
        "actor_id": actor_id,
        "actor_seq": next(_seq),
        "op": "patch",
        "payload": dict(payload),
    }


def test_healthz_needs_no_token(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_v1_requires_a_bearer_token(client: TestClient) -> None:
    assert client.get("/v1/events").status_code == 401
    assert (
        client.post("/v1/session", json={"sync_version": 1, "schema_version": 1}).status_code == 401
    )


def test_invalid_token_is_rejected(client: TestClient) -> None:
    r = client.get("/v1/events", headers={"authorization": "Bearer kq_bogus.nope"})
    assert r.status_code == 401


def test_commit_pull_snapshot_round_trip_over_http(client: TestClient, token: str) -> None:
    auth = {"authorization": f"Bearer {token}"}

    # session handshake
    s = client.post("/v1/session", json={"sync_version": 1, "schema_version": 1}, headers=auth)
    assert s.status_code == 200 and s.json()["sync_version"] == 1

    # commit (pre-cutover / unsigned for the round-trip; auth still binds actor)
    body = {"events": [_wire(MEMBER, title="A", status="todo")], "require_signature": False}
    c = client.post("/v1/events", json=body, headers=auth)
    assert c.status_code == 200, c.text
    assert c.json()[0]["status"] == "committed"

    # pull it back
    pulled = client.get("/v1/events", headers=auth).json()
    assert len(pulled) == 1 and pulled[0]["payload"]["title"] == "A"

    # snapshot folds it
    snap = client.get("/v1/snapshot", params={"collection": "tickets"}, headers=auth).json()
    assert snap["tkt_srv00".ljust(26, "0")]["status"] == "todo"


def test_actor_must_be_the_authenticated_member(client: TestClient, token: str) -> None:
    """Caller-binding: a member cannot submit an event as someone else."""
    auth = {"authorization": f"Bearer {token}"}
    body = {
        "events": [_wire("mbr_someone_else000000000", title="forged")],
        "require_signature": False,
    }
    r = client.post("/v1/events", json=body, headers=auth)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "policy_denied"
    # and nothing committed
    assert client.get("/v1/events", headers=auth).json() == []


def test_ack_is_bound_to_the_authenticated_member_not_the_body(
    pg_engine: Engine, client: TestClient, token: str
) -> None:
    """[SEC red-team] A member cannot move a peer's ack watermark.

    The body's member_id is untrusted; the server writes the ack row under the
    authenticated member. Without this binding any member could overwrite a
    peer's watermark and (once self-hosted retention compaction lands) trigger
    premature pruning of sync_events a lagging replica still needs — the
    cross-member stranding the Supabase RLS path prevents."""
    from sqlalchemy import select as sa_select

    from kantaq_backend_postgres.schema import sync_acks

    auth = {"authorization": f"Bearer {token}"}
    forged = {"member_id": "mbr_victim00000000000000", "replica_id": "dev_x", "acked_rev": 999}
    assert client.post("/v1/acks", json=forged, headers=auth).status_code == 200

    with pg_engine.connect() as conn:
        rows = conn.execute(sa_select(sync_acks.c.member_id, sync_acks.c.acked_rev)).all()
    # exactly one row, owned by the AUTHENTICATED member — not the forged victim
    assert [(r.member_id, r.acked_rev) for r in rows] == [(MEMBER, 999)]


def test_client_cannot_relax_the_server_signature_floor(pg_engine: Engine, token: str) -> None:
    """[SEC red-team] A client must not bypass the grant check by sending
    require_signature=false against a cut-over server.

    The no-RLS gap: without a server-owned floor, a member could commit unsigned,
    grant-less writes by relaxing require_signature. A server cut over to signing
    (require_signature=True) must reject the unsigned event regardless of the
    client's flag — the floor only ratchets up. (The standing guard for this
    attack class lives here, not the MCP gateway's ATTACK_CATALOG, because the
    sync-server is a distinct surface.)"""
    strict = TestClient(create_app(pg_engine, require_signature=True))
    auth = {"authorization": f"Bearer {token}"}
    body = {
        "events": [_wire(MEMBER, title="sneaky unsigned write")],
        "require_signature": False,  # the client tries to relax the floor
    }
    r = strict.post("/v1/events", json=body, headers=auth)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unsigned"
    # nothing committed: the floor held
    assert strict.get("/v1/events", headers=auth).json() == []
