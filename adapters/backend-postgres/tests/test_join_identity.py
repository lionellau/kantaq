"""DEBT-42: a real bootstrapped runtime joins a self-hosted backend and pushes.

The E25 compose smoke proved commit→pull by **stamping** ``actor_id = the seeded
member id`` directly — which hid the onboarding gap: a real runtime authors
events as its **own local Owner**, which never equalled the seeded member, so
every push was rejected (``actor is not the authenticated member``).

This drives the REAL path end to end: the runtime resolves the seeded member from
the server (``whoami``), **adopts** it as its local Owner (``adopt_owner`` — the
``kantaq sync login`` machinery), authors an event AS that Owner, and pushes →
committed. The negative half proves the caller-binding wall still holds for a
runtime that has **not** adopted the seeded member (no impersonation).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_backend_postgres import SyncBackendError, SyncServerBackend, create_app
from kantaq_core.identity import IdentityService
from kantaq_core.identity.tokens import mint_token
from kantaq_db.models import Member, Token
from kantaq_db.session import get_engine, sqlite_url
from kantaq_protocol import Event

from .conftest import WORKSPACE_ID

SERVER_URL = "http://testserver"
SEEDED_MEMBER = "mbr_join00".ljust(26, "0")


@pytest.fixture
def app_client(pg_engine: Engine) -> TestClient:
    """An httpx client wired to the real ASGI sync-server."""
    return TestClient(create_app(pg_engine))


@pytest.fixture
def seeded_token(pg_engine: Engine) -> str:
    """A founding member + token on the backend — what ``seed`` mints."""
    token_id = "tok_join00".ljust(26, "0")
    plaintext, phc = mint_token(token_id)
    with Session(pg_engine) as session:
        session.add(
            Member(
                id=SEEDED_MEMBER,
                workspace_id=WORKSPACE_ID,
                email="founder@acme.dev",
                role="Owner",
            )
        )
        session.flush()
        session.add(Token(id=token_id, member_id=SEEDED_MEMBER, hashed=phc))
        session.commit()
    return plaintext


@pytest.fixture
def runtime_engine(tmp_path: object) -> Engine:
    """A FRESH local runtime replica — schema only, no identity yet."""
    engine = get_engine(sqlite_url(str(tmp_path / "runtime.sqlite")))  # type: ignore[operator]
    SQLModel.metadata.create_all(engine)
    return engine


def _ticket_event(actor_id: str, *, n: int, title: str) -> Event:
    return Event(
        event_id=f"e{n:025d}",
        collection="tickets",
        entity_id="tkt_join00".ljust(26, "0"),
        actor_id=actor_id,
        actor_seq=n,
        op="patch",
        base_rev=None,
        policy_ref=None,
        payload={"title": title},
        sig=None,
    )


def test_a_fresh_runtime_adopts_the_seeded_member_then_pushes(
    app_client: TestClient, seeded_token: str, runtime_engine: Engine
) -> None:
    backend = SyncServerBackend(SERVER_URL, seeded_token, client=app_client)

    # 1. Resolve who the token is, and adopt that member as the local Owner —
    #    the real `kantaq sync login` path, NOT a stamped actor_id.
    me = backend.whoami()
    assert me["member_id"] == SEEDED_MEMBER
    with Session(runtime_engine) as session:
        minted = IdentityService(session).adopt_owner(
            member_id=me["member_id"],
            workspace_id=me["workspace_id"],
            email=me["email"],
            workspace_name=me["workspace_name"],
        )
    assert minted is not None

    # 2. The runtime's Owner IS now the seeded member, so it authors AS that id.
    with Session(runtime_engine) as session:
        owner = session.exec(select(Member)).one()
    assert owner.id == SEEDED_MEMBER

    # 3. An event authored by the adopted Owner pushes and commits — the thing the
    #    old, stamped smoke faked. actor_id is read from the local Owner, not a
    #    hardcoded constant.
    out = backend.commit_events(
        [_ticket_event(owner.id, n=1, title="from a real runtime")],
        require_signature=False,
    )
    assert out[0].status == "committed"


def test_a_runtime_that_has_not_joined_is_rejected(
    app_client: TestClient, seeded_token: str
) -> None:
    """The caller-binding wall still holds (SEC): an event authored by some OTHER
    member — a runtime that never adopted the seeded identity — is denied, so the
    fix unifies identities without weakening the impersonation guard."""
    backend = SyncServerBackend(SERVER_URL, seeded_token, client=app_client)
    stranger = "mbr_stranger".ljust(26, "0")
    with pytest.raises(SyncBackendError, match="actor is not the authenticated member"):
        backend.commit_events(
            [_ticket_event(stranger, n=2, title="impersonation attempt")],
            require_signature=False,
        )
