"""Supabase Auth (GoTrue) client: email magic link (E24-T2, FR-E24-1).

A thin, typed client over the documented GoTrue REST endpoints — the official
``supabase-py`` stack does not clear the project's reuse bar (see
``docs/stack.md``), and v0.0.5 needs exactly four calls:

- ``request_magic_link``  POST /auth/v1/otp        (sends the email)
- ``verify``              POST /auth/v1/verify      (6-digit code → session)
- ``refresh``             POST /auth/v1/token?grant_type=refresh_token
- ``get_user``            GET  /auth/v1/user        (whoami for a JWT)
- ``sign_out``            POST /auth/v1/logout      (revokes the refresh token)

Security posture (SEC task):

- constructed with the **anon key only** — a service-role key raises
  (``assert_client_safe_key``, NFR-E24-1);
- error text from the backend is surfaced, but no exception or repr ever
  carries the api key or a token;
- the HTTP client is injectable so tests stay hermetic (httpx MockTransport,
  the same pattern as the runtime's connection verify).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from kantaq_backend_supabase.keys import assert_client_safe_key

_TIMEOUT = 10.0


class AuthError(Exception):
    """A GoTrue call failed; carries the backend's message and HTTP status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class User:
    """The authenticated Supabase user (subset kantaq reads)."""

    id: str
    email: str


@dataclass(frozen=True)
class Session:
    """An authenticated session. ``access_token`` is the user JWT RLS sees."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    user: User


def _error_message(response: httpx.Response) -> str:
    """The backend's error text, without echoing anything secret."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    for key in ("msg", "message", "error_description", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return f"HTTP {response.status_code}"


def _parse_session(payload: dict[str, Any]) -> Session:
    user = payload.get("user") or {}
    return Session(
        access_token=str(payload.get("access_token", "")),
        refresh_token=str(payload.get("refresh_token", "")),
        expires_in=int(payload.get("expires_in", 0)),
        user=User(id=str(user.get("id", "")), email=str(user.get("email", ""))),
    )


class SupabaseAuth:
    """GoTrue over httpx. One instance per configured backend."""

    def __init__(
        self,
        url: str,
        anon_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = url.rstrip("/") + "/auth/v1"
        self._anon_key = assert_client_safe_key(anon_key)
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def _headers(self, bearer: str | None = None) -> dict[str, str]:
        # The anon key authenticates the *project*; the user JWT (when present)
        # authenticates the member. Same scheme PostgREST sees later.
        return {
            "apikey": self._anon_key,
            "Authorization": f"Bearer {bearer or self._anon_key}",
        }

    def _post(self, path: str, json: dict[str, Any], bearer: str | None = None) -> httpx.Response:
        response = self._client.post(
            f"{self._base}{path}", json=json, headers=self._headers(bearer)
        )
        if response.status_code >= 400:
            raise AuthError(_error_message(response), response.status_code)
        return response

    def request_magic_link(self, email: str, *, create_user: bool = False) -> None:
        """Email a magic link / one-time code to ``email``.

        ``create_user=False`` keeps sign-in invite-only: an email that has no
        Supabase user gets an error, not an account (members are invited by a
        maintainer, E06). GoTrue returns 200 with an empty body on success.
        """
        self._post("/otp", {"email": email, "create_user": create_user})

    def verify(self, email: str, token: str) -> Session:
        """Exchange the emailed one-time code for a session.

        ``type=email`` covers both the 6-digit code and the token embedded in
        the magic-link URL (GoTrue verifies them identically server-side).
        """
        response = self._post("/verify", {"type": "email", "email": email, "token": token})
        return _parse_session(response.json())

    def refresh(self, refresh_token: str) -> Session:
        """Rotate a session before the access token expires."""
        response = self._post("/token?grant_type=refresh_token", {"refresh_token": refresh_token})
        return _parse_session(response.json())

    def get_user(self, access_token: str) -> User:
        """Whoami for an access token (also proves the token is still valid)."""
        response = self._client.get(f"{self._base}/user", headers=self._headers(access_token))
        if response.status_code >= 400:
            raise AuthError(_error_message(response), response.status_code)
        payload = response.json()
        return User(id=str(payload.get("id", "")), email=str(payload.get("email", "")))

    def sign_out(self, access_token: str) -> None:
        """Revoke the session's refresh token server-side."""
        self._post("/logout", {}, bearer=access_token)
