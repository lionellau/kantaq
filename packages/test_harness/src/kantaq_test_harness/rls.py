"""RLS test doubles for the Backend profile (MOD-30, lands with MOD-05/E24).

Two pieces the Supabase (and later self-hosted Postgres, MOD-28) backend tests
lean on:

- ``install_supabase_auth_stub`` — recreates, on a plain Postgres, exactly the
  ambient environment Supabase gives RLS policies: the ``anon`` /
  ``authenticated`` / ``service_role`` roles and the ``auth.jwt()`` /
  ``auth.uid()`` / ``auth.role()`` helpers reading the ``request.jwt.claims``
  setting the way PostgREST populates it. The stub mirrors Supabase's
  documented definitions so policies tested here behave identically there.

- ``TamperedClient`` — the standard's tampered client: it skips every
  app-layer check and talks straight to Postgres under a chosen role with
  chosen (i.e. forged-at-will) JWT claims. If RLS is the only thing standing,
  these tests prove it stands (D-03's coarse layer; fail-closed principle 7).

Like ``db.py``, this module never imports the ORM — the harness stays a leaf
dependency. Pair with ``EphemeralPostgres``; both are opt-in via
``KANTAQ_TEST_POSTGRES_URL``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, Row

# The three roles PostgREST switches between. SET ROLE is interpolated, so the
# allowlist is the injection guard.
SUPABASE_ROLES = ("anon", "authenticated", "service_role")

# Mirrors Supabase's environment: roles (service_role carries BYPASSRLS, as in
# Supabase) and the auth.* claim helpers, defined the way Supabase defines them
# (https://supabase.com/docs/guides/database/postgres/row-level-security).
# Roles are cluster-global, so creation tolerates an existing role; the grant
# to session_user lets a non-superuser test connection SET ROLE into them.
_AUTH_STUB = """
do $$ begin create role anon nologin; exception when duplicate_object then null; end $$;
do $$ begin create role authenticated nologin; exception when duplicate_object then null; end $$;
do $$ begin
  create role service_role nologin bypassrls;
exception when duplicate_object then null; end $$;

do $$ begin
  execute format('grant anon, authenticated, service_role to %I', session_user);
end $$;

create schema if not exists auth;

-- Supabase lets every API role call the auth.* helpers (policies running
-- under `authenticated` call auth.jwt() directly); mirror that here.
grant usage on schema auth to anon, authenticated, service_role;

-- Supabase grants the API roles usage on public and pre-configures DEFAULT
-- PRIVILEGES so every newly created public table is auto-granted ALL to all
-- three roles. Mirror that too — it is load-bearing: the policies file must
-- explicitly strip anon/authenticated back to its documented ceiling, and
-- without this line the stub would (and once did) hide an over-grant that
-- was live in production.
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public
  grant all on tables to anon, authenticated, service_role;

create or replace function auth.jwt() returns jsonb
language sql stable
as $fn$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  )
$fn$;

create or replace function auth.uid() returns uuid
language sql stable
as $fn$
  select nullif(auth.jwt() ->> 'sub', '')::uuid
$fn$;

create or replace function auth.role() returns text
language sql stable
as $fn$
  select auth.jwt() ->> 'role'
$fn$;
"""


def install_supabase_auth_stub(engine: Engine) -> None:
    """Install the Supabase auth environment on a plain Postgres database."""
    with engine.begin() as conn:
        conn.execute(text(_AUTH_STUB))


def apply_sql(engine: Engine, sql: str) -> None:
    """Apply a SQL artifact (a migration or policy file) in one transaction."""
    with engine.begin() as conn:
        conn.execute(text(sql))


def supabase_claims(
    email: str,
    *,
    sub: str = "00000000-0000-0000-0000-000000000001",
    role: str = "authenticated",
    **extra: Any,
) -> dict[str, Any]:
    """JWT claims shaped the way a Supabase access token carries them."""
    return {"sub": sub, "email": email, "role": role, **extra}


@dataclass(frozen=True)
class Attempt:
    """Outcome of a tampered write attempt: did it stick, and how was it denied?

    RLS denies in two shapes — assert on ``denied``, not the shape:
    - a violated ``WITH CHECK`` (or a missing grant) raises → ``error``;
    - a ``USING`` filter silently matches nothing → ``ok`` with ``rowcount`` 0.
    """

    ok: bool
    rowcount: int = 0
    error: str = ""

    @property
    def denied(self) -> bool:
        return (not self.ok) or self.rowcount == 0


@dataclass
class TamperedClient:
    """Direct Postgres access under a Supabase role with arbitrary claims.

    The adversary of the harness standard: a client with valid-looking
    credentials that ignores the app entirely. Connections run with
    ``request.jwt.claims`` set session-wide and ``SET ROLE`` applied, exactly
    like a PostgREST request — except nothing here is constrained by the app.
    """

    engine: Engine
    claims: Mapping[str, Any] = field(default_factory=dict)
    role: str = "authenticated"

    def __post_init__(self) -> None:
        if self.role not in SUPABASE_ROLES:
            raise ValueError(f"role must be one of {SUPABASE_ROLES}, got {self.role!r}")

    @contextmanager
    def session(self) -> Iterator[Connection]:
        """A connection with the forged claims + role applied (caller commits).

        The connection is scrubbed (rollback, RESET ROLE, cleared claims)
        before it returns to the pool, so one client's identity can never
        leak into the next checkout.
        """
        with self.engine.connect() as conn:
            try:
                conn.execute(
                    text("select set_config('request.jwt.claims', :claims, false)"),
                    {"claims": json.dumps(dict(self.claims))},
                )
                conn.execute(text(f'set role "{self.role}"'))
                yield conn
            finally:
                conn.rollback()
                conn.execute(text("reset role"))
                conn.execute(text("select set_config('request.jwt.claims', '', false)"))
                conn.commit()

    def fetch_all(self, sql: str, **params: Any) -> list[Row[Any]]:
        """Run a SELECT; RLS decides what comes back."""
        with self.session() as conn:
            return list(conn.execute(text(sql), params))

    def attempt(self, sql: str, **params: Any) -> Attempt:
        """Attempt a write; commit if Postgres lets it through."""
        with self.session() as conn:
            try:
                result = conn.execute(text(sql), params)
            except Exception as exc:  # noqa: BLE001 — the error shape is the result
                conn.rollback()
                return Attempt(ok=False, error=str(exc))
            conn.commit()
            return Attempt(ok=True, rowcount=max(result.rowcount, 0))
