"""Anon-key guard: the service-role key never reaches a client (NFR-E24-1).

Supabase API keys are JWTs whose ``role`` claim is ``anon`` or ``service_role``.
The runtime is a *client* of the shared backend — it must only ever hold the
anon key. ``assert_client_safe_key`` makes that structural: pasting the
service-role key into ``SUPABASE_ANON_KEY`` refuses at construction time instead
of silently arming a client with RLS-bypassing credentials.

Claims are read without signature verification — the client has no signing
secret (that is the point), and the guard is a local misconfiguration check,
not an authentication step. Supabase itself verifies every key server-side.
"""

from __future__ import annotations

import jwt


class ServiceRoleKeyError(ValueError):
    """A service-role key was offered where only the anon key is allowed."""


def key_role(api_key: str) -> str | None:
    """The ``role`` claim of a Supabase API key, or None if unreadable."""
    try:
        claims = jwt.decode(api_key, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    role = claims.get("role")
    return role if isinstance(role, str) else None


def assert_client_safe_key(api_key: str) -> str:
    """Return ``api_key`` if it is safe for a client; raise on a service key.

    Keys whose role cannot be read are allowed through (Supabase's newer
    publishable keys are not JWTs); the backend still rejects bad keys. Only a
    positively identified ``service_role`` key is refused.
    """
    if key_role(api_key) == "service_role":
        raise ServiceRoleKeyError(
            "this is the Supabase service-role key; clients must use the anon key "
            "(the service-role key never leaves the backend, NFR-E24-1)"
        )
    return api_key
