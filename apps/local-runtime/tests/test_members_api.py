"""Members API (E06-T3): invite, list, revoke, rotate over HTTP.

Pins the role matrix at the HTTP surface, the last-Owner guard, and
NFR-E06-1: no secret material in any response except the one-time mint.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, MintedToken, TokenVerifier
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _invite(client: TestClient, as_token: str, email: str, role: str) -> dict[str, object]:
    response = client.post(
        "/v1/members/invite", json={"email": email, "role": role}, headers=_bearer(as_token)
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def test_invite_then_list_shows_member_without_secrets(
    client: TestClient, owner: MintedToken
) -> None:
    invited = _invite(client, owner.plaintext, "new@team.dev", "Member")
    member = invited["member"]
    assert isinstance(member, dict)
    assert member["email"] == "new@team.dev"
    assert member["status"] == "invited"

    listing = client.get("/v1/members", headers=_bearer(owner.plaintext))
    emails = [m["email"] for m in listing.json()]
    assert emails == ["owner@local", "new@team.dev"]


def test_invited_token_works_and_first_use_activates(
    client: TestClient, owner: MintedToken
) -> None:
    invited = _invite(client, owner.plaintext, "new@team.dev", "Member")
    token = str(invited["token"])
    response = client.get("/v1/members", headers=_bearer(token))
    assert response.status_code == 200
    me = next(m for m in response.json() if m["email"] == "new@team.dev")
    assert me["status"] == "active"  # first authenticated call flips invited→active


def test_viewer_and_member_cannot_invite_or_revoke(client: TestClient, owner: MintedToken) -> None:
    for role in ("Viewer", "Member"):
        token = str(_invite(client, owner.plaintext, f"{role.lower()}@team.dev", role)["token"])
        assert client.get("/v1/members", headers=_bearer(token)).status_code == 200
        denied_invite = client.post(
            "/v1/members/invite",
            json={"email": "x@team.dev", "role": "Member"},
            headers=_bearer(token),
        )
        assert denied_invite.status_code == 403
        denied_revoke = client.post("/v1/members/someid/revoke", headers=_bearer(token))
        assert denied_revoke.status_code == 403


def test_agent_scope_gates_reads(client: TestClient, owner: MintedToken) -> None:
    scoped = client.post(
        "/v1/members/invite",
        json={"email": "bot@team.dev", "role": "Agent", "scopes": ["members.read"]},
        headers=_bearer(owner.plaintext),
    )
    assert scoped.status_code == 201
    bot_token = scoped.json()["token"]
    assert client.get("/v1/members", headers=_bearer(bot_token)).status_code == 200

    unscoped = client.post(
        "/v1/members/invite",
        json={"email": "bot2@team.dev", "role": "Agent"},
        headers=_bearer(owner.plaintext),
    )
    bot2_token = unscoped.json()["token"]
    assert client.get("/v1/members", headers=_bearer(bot2_token)).status_code == 403


def test_revoke_member_rejects_their_token_immediately(
    client: TestClient, owner: MintedToken
) -> None:
    invited = _invite(client, owner.plaintext, "leaver@team.dev", "Member")
    member = invited["member"]
    assert isinstance(member, dict)
    leaver_token = str(invited["token"])
    assert client.get("/v1/members", headers=_bearer(leaver_token)).status_code == 200

    revoked = client.post(f"/v1/members/{member['id']}/revoke", headers=_bearer(owner.plaintext))
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    # Same-process revoke purges the verify cache: rejected on the very next call.
    assert client.get("/v1/members", headers=_bearer(leaver_token)).status_code == 401


def test_last_owner_cannot_be_revoked(client: TestClient, owner: MintedToken) -> None:
    response = client.post(
        f"/v1/members/{owner.member_id}/revoke", headers=_bearer(owner.plaintext)
    )
    assert response.status_code == 409


def test_revoke_unknown_member_is_404(client: TestClient, owner: MintedToken) -> None:
    response = client.post(
        "/v1/members/01UNKNOWNMEMBER00000000000/revoke", headers=_bearer(owner.plaintext)
    )
    assert response.status_code == 404


def test_rotate_own_token_old_rejected_new_accepted(client: TestClient, owner: MintedToken) -> None:
    rotated = client.post(f"/v1/members/{owner.member_id}/rotate", headers=_bearer(owner.plaintext))
    assert rotated.status_code == 200
    new_token = rotated.json()["token"]
    assert client.get("/v1/members", headers=_bearer(owner.plaintext)).status_code == 401
    assert client.get("/v1/members", headers=_bearer(new_token)).status_code == 200


def test_member_may_rotate_self_but_not_others(client: TestClient, owner: MintedToken) -> None:
    invited = _invite(client, owner.plaintext, "regular@team.dev", "Member")
    member = invited["member"]
    assert isinstance(member, dict)
    member_token = str(invited["token"])

    own = client.post(f"/v1/members/{member['id']}/rotate", headers=_bearer(member_token))
    assert own.status_code == 200

    other = client.post(
        f"/v1/members/{owner.member_id}/rotate",
        headers=_bearer(str(own.json()["token"])),
    )
    assert other.status_code == 403


def test_no_secret_material_in_any_response(client: TestClient, owner: MintedToken) -> None:
    """NFR-E06-1: hashes never leave the server; plaintext only at mint time."""
    invited = _invite(client, owner.plaintext, "new@team.dev", "Member")
    one_time = str(invited["token"])

    listing = client.get("/v1/members", headers=_bearer(owner.plaintext))
    rotated = client.post(f"/v1/members/{owner.member_id}/rotate", headers=_bearer(owner.plaintext))
    bodies = listing.text + rotated.text
    assert "$argon2id$" not in bodies  # no stored hash material, ever
    assert "hashed" not in listing.text
    assert one_time not in bodies  # a minted token never reappears later


def test_openapi_schema_carries_no_secret_fields(client: TestClient) -> None:
    schema = client.get("/openapi.json")
    member_out = schema.json()["components"]["schemas"]["MemberOut"]
    assert "hashed" not in member_out["properties"]
