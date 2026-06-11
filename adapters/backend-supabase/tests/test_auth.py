"""SupabaseAuth (GoTrue) client tests — hermetic via httpx.MockTransport (E24-T2).

The transport records every request, so the tests pin both behavior and the
security posture: which endpoint, which headers, which keys — and which never
appear anywhere.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest

from kantaq_backend_supabase.auth import AuthError, SupabaseAuth
from kantaq_backend_supabase.keys import ServiceRoleKeyError

_SECRET = "test-only-signing-secret-0123456789abcdef"
ANON_KEY = jwt.encode({"role": "anon"}, _SECRET, algorithm="HS256")
SERVICE_KEY = jwt.encode({"role": "service_role"}, _SECRET, algorithm="HS256")
URL = "https://acme.supabase.co"

SESSION_PAYLOAD: dict[str, Any] = {
    "access_token": "user-jwt-access",
    "refresh_token": "user-jwt-refresh",
    "expires_in": 3600,
    "user": {"id": "9c0c0f2e-0000-0000-0000-000000000001", "email": "alice@acme.dev"},
}


class RecordingBackend:
    """A scripted GoTrue: returns canned responses, records every request."""

    def __init__(self, responses: dict[str, httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.get(request.url.path, httpx.Response(200, json={}))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def make_auth(backend: RecordingBackend, key: str = ANON_KEY) -> SupabaseAuth:
    return SupabaseAuth(URL, key, client=backend.client())


def test_magic_link_posts_to_otp_with_the_anon_key() -> None:
    backend = RecordingBackend()
    make_auth(backend).request_magic_link("alice@acme.dev")
    (request,) = backend.requests
    assert request.url.path == "/auth/v1/otp"
    assert request.headers["apikey"] == ANON_KEY
    assert request.headers["Authorization"] == f"Bearer {ANON_KEY}"
    assert json.loads(request.content) == {"email": "alice@acme.dev", "create_user": False}


def test_magic_link_is_invite_only_by_default() -> None:
    """create_user=False: an uninvited email gets an error, not an account."""
    backend = RecordingBackend()
    make_auth(backend).request_magic_link("alice@acme.dev")
    assert json.loads(backend.requests[0].content)["create_user"] is False


def test_verify_exchanges_the_emailed_code_for_a_session() -> None:
    backend = RecordingBackend({"/auth/v1/verify": httpx.Response(200, json=SESSION_PAYLOAD)})
    session = make_auth(backend).verify("alice@acme.dev", "123456")
    assert json.loads(backend.requests[0].content) == {
        "type": "email",
        "email": "alice@acme.dev",
        "token": "123456",
    }
    assert session.access_token == "user-jwt-access"
    assert session.refresh_token == "user-jwt-refresh"
    assert session.expires_in == 3600
    assert session.user.email == "alice@acme.dev"


def test_verify_surfaces_the_backend_error() -> None:
    backend = RecordingBackend(
        {"/auth/v1/verify": httpx.Response(403, json={"msg": "Token has expired or is invalid"})}
    )
    with pytest.raises(AuthError) as excinfo:
        make_auth(backend).verify("alice@acme.dev", "000000")
    assert excinfo.value.status_code == 403
    assert "expired or is invalid" in str(excinfo.value)


def test_refresh_rotates_the_session() -> None:
    backend = RecordingBackend({"/auth/v1/token": httpx.Response(200, json=SESSION_PAYLOAD)})
    session = make_auth(backend).refresh("old-refresh-token")
    request = backend.requests[0]
    assert request.url.path == "/auth/v1/token"
    assert request.url.params["grant_type"] == "refresh_token"
    assert json.loads(request.content) == {"refresh_token": "old-refresh-token"}
    assert session.access_token == "user-jwt-access"


def test_get_user_sends_the_user_jwt_as_bearer() -> None:
    backend = RecordingBackend({"/auth/v1/user": httpx.Response(200, json=SESSION_PAYLOAD["user"])})
    user = make_auth(backend).get_user("user-jwt-access")
    request = backend.requests[0]
    assert request.headers["Authorization"] == "Bearer user-jwt-access"
    assert request.headers["apikey"] == ANON_KEY
    assert user.email == "alice@acme.dev"


def test_sign_out_revokes_with_the_user_jwt() -> None:
    backend = RecordingBackend({"/auth/v1/logout": httpx.Response(204)})
    make_auth(backend).sign_out("user-jwt-access")
    request = backend.requests[0]
    assert request.url.path == "/auth/v1/logout"
    assert request.headers["Authorization"] == "Bearer user-jwt-access"


def test_client_refuses_a_service_role_key() -> None:
    """NFR-E24-1 made structural: the client cannot exist with the service key."""
    with pytest.raises(ServiceRoleKeyError):
        SupabaseAuth(URL, SERVICE_KEY)


def test_only_the_anon_key_ever_travels() -> None:
    """Every header on every request carries the anon key and nothing else."""
    backend = RecordingBackend(
        {
            "/auth/v1/verify": httpx.Response(200, json=SESSION_PAYLOAD),
            "/auth/v1/user": httpx.Response(200, json=SESSION_PAYLOAD["user"]),
        }
    )
    auth = make_auth(backend)
    auth.request_magic_link("alice@acme.dev")
    auth.verify("alice@acme.dev", "123456")
    auth.get_user("user-jwt-access")
    for request in backend.requests:
        for value in request.headers.values():
            assert SERVICE_KEY not in value


def test_auth_error_never_carries_key_material() -> None:
    backend = RecordingBackend({"/auth/v1/otp": httpx.Response(500, content=b"not json")})
    with pytest.raises(AuthError) as excinfo:
        make_auth(backend).request_magic_link("alice@acme.dev")
    assert ANON_KEY not in str(excinfo.value)
    assert str(excinfo.value) == "HTTP 500"


def test_session_repr_hides_the_tokens() -> None:
    backend = RecordingBackend({"/auth/v1/verify": httpx.Response(200, json=SESSION_PAYLOAD)})
    session = make_auth(backend).verify("alice@acme.dev", "123456")
    assert "user-jwt-access" not in repr(session)
    assert "user-jwt-refresh" not in repr(session)
