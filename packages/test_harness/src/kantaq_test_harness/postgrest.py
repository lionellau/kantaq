"""FakePostgREST — Supabase's data API on an ephemeral Postgres (MOD-30).

The Backend profile's missing piece for E24-T4: the sync adapter (MOD-05)
speaks PostgREST's REST dialect over httpx, and hermetic tests need that
dialect answered by a *real* Postgres running the *checked-in* SQL artifacts
— real RLS, real identity-assigned revisions — not by a canned response.

``FakePostgREST.handler`` is an ``httpx.MockTransport`` handler that does, for
the subset of the dialect kantaq uses, exactly what PostgREST documents:

1. read the ``Authorization`` bearer JWT (unverified, like the auth stub —
   the signature check is Supabase's job, not the contract under test),
   reject an expired one (401), and take the Postgres role from its ``role``
   claim (no/odd JWT → ``anon``);
2. apply the claims and ``SET ROLE`` exactly as PostgREST does (reusing
   ``TamperedClient``'s scrubbed sessions);
3. translate the request to SQL — ``GET`` filters ``col=eq.v`` / ``col=gt.v``
   with ``order``/``limit``, ``POST`` bulk insert with ``?on_conflict=..`` and
   ``Prefer: resolution=ignore-duplicates`` becoming
   ``INSERT .. ON CONFLICT (..) DO NOTHING``, ``return=representation``
   becoming ``RETURNING *``;
4. map Postgres errors to PostgREST's status/body shapes (42501 → 403,
   23505/23503 → 409, other constraint violations → 400).

Pair with ``EphemeralPostgres`` + ``install_supabase_auth_stub`` + the
checked-in migration/policy files, the same way the RLS suite does. Identifier
inputs (table, columns) are allowlist-validated; values travel as bound
parameters. ``now`` is injectable (FakeClock) so the ``exp`` check stays
deterministic.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from kantaq_test_harness.rls import SUPABASE_ROLES, TamperedClient

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

# The RPC functions this fake exposes, with their named-argument signatures in
# call order. PostgREST passes the JSON body as named function arguments; we
# mirror that for the kantaq atomic commit RPC (E24-T6). The function returns a
# JSON value, which PostgREST returns as the response body. ``cast`` "" binds
# the value as-is. Module-level so the dataclass below has no mutable field.
_RPC_ARGS: dict[str, tuple[tuple[str, str], ...]] = {
    # p_cas (E05-T3): the compare-and-swap flag — when true the RPC raises
    # rebase_required (sqlstate 40001) instead of committing a contending write.
    # Passed through so the fake can exercise the adapter's CAS path (test_cas_live).
    "events": (("p_events", "jsonb"), ("p_require_signature", ""), ("p_cas", "boolean")),
}

# PostgREST → HTTP status for the SQLSTATEs this fake can produce.
_SQLSTATE_STATUS = {
    "42501": 403,  # insufficient_privilege (incl. RLS WITH CHECK violations)
    "23505": 409,  # unique_violation
    "23503": 409,  # foreign_key_violation
}


def _b64url_json(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    decoded: Any = json.loads(base64.urlsafe_b64decode(padded))
    return decoded if isinstance(decoded, dict) else {}


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """The JWT payload, unverified — how the stubbed environment reads claims."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        return _b64url_json(parts[1])
    except (ValueError, binascii.Error):
        return {}


def encode_test_jwt(claims: Mapping[str, Any]) -> str:
    """An unsigned JWT carrying ``claims`` — what tests hand the adapter as a
    session token. Pair with ``supabase_claims`` from ``rls``. Deliberately
    unverifiable: signature checking is Supabase's job, never the client's."""

    def segment(obj: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(obj), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}.test-signature"


def _identifier(name: str, what: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe {what} identifier: {name!r}")
    return name


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass
class FakePostgREST:
    """Serve ``/rest/v1/<table>`` requests against a real (ephemeral) Postgres."""

    engine: Engine
    now: Callable[[], float] | None = None
    base_url: str = "https://fake.supabase.test"
    requests: list[httpx.Request] = field(default_factory=list)

    # ------------------------------------------------------------- transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url=self.base_url)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "apikey" not in request.headers:
            return self._error(401, None, "No API key found in request")

        claims = self._claims(request)
        exp = claims.get("exp")
        if self.now is not None and isinstance(exp, int | float) and exp <= self.now():
            return self._error(401, "PGRST301", "JWT expired")
        role = claims.get("role")
        if not isinstance(role, str) or role not in SUPABASE_ROLES:
            role = "anon"

        rpc_match = re.fullmatch(r"/rest/v1/rpc/([^/]+)", request.url.path)
        if rpc_match is not None:
            try:
                sql_client = TamperedClient(self.engine, claims=claims, role=role)
                return self._rpc(sql_client, _identifier(rpc_match.group(1), "function"), request)
            except DBAPIError as exc:
                return self._pg_error(exc)
            except ValueError as exc:
                return self._error(400, None, str(exc))

        match = re.fullmatch(r"/rest/v1/([^/]+)", request.url.path)
        if match is None:
            return self._error(404, None, f"no route for {request.url.path}")
        try:
            table = _identifier(match.group(1), "table")
            params = dict(parse_qsl(request.url.query.decode()))
            sql_client = TamperedClient(self.engine, claims=claims, role=role)
            if request.method == "GET":
                return self._select(sql_client, table, params)
            if request.method == "POST":
                return self._insert(sql_client, table, params, request)
        except DBAPIError as exc:
            return self._pg_error(exc)
        except ValueError as exc:
            return self._error(400, None, str(exc))
        return self._error(405, None, f"{request.method} not supported by the fake")

    # ------------------------------------------------------------------ auth

    @staticmethod
    def _claims(request: httpx.Request) -> dict[str, Any]:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return {}
        return decode_jwt_claims(token.strip())

    # ------------------------------------------------------------- RPC (v0.2)

    def _rpc(
        self, sql_client: TamperedClient, function: str, request: httpx.Request
    ) -> httpx.Response:
        if request.method != "POST":
            return self._error(405, None, f"{request.method} not supported for rpc")
        signature = _RPC_ARGS.get(function)
        if signature is None:
            return self._error(404, None, f"no rpc {function!r} in the fake")
        body = json.loads(request.content) if request.content else {}
        if not isinstance(body, dict):
            raise ValueError("rpc body must be a JSON object of named arguments")
        placeholders: list[str] = []
        bound: dict[str, Any] = {}
        for arg, cast in signature:
            value = body.get(arg)
            if isinstance(value, dict | list):
                bound[arg] = json.dumps(value)
            else:
                bound[arg] = value
            placeholders.append(f"cast(:{arg} as {cast})" if cast else f":{arg}")
        sql = f"select public.{function}({', '.join(placeholders)})"  # noqa: S608 - allowlisted
        with sql_client.session() as conn:
            result = conn.execute(text(sql), bound).scalar()
            conn.commit()
        payload = result if isinstance(result, list) else ([] if result is None else result)
        return httpx.Response(
            200,
            content=json.dumps(payload, default=_json_default),
            headers={"Content-Type": "application/json"},
        )

    # ------------------------------------------------------------------- GET

    def _select(
        self, sql_client: TamperedClient, table: str, params: dict[str, str]
    ) -> httpx.Response:
        clauses: list[str] = []
        bound: dict[str, Any] = {}
        order_sql = ""
        limit_sql = ""
        for key, raw in params.items():
            if key == "select":
                continue  # the fake always returns every column
            if key == "order":
                column, _, direction = raw.partition(".")
                order_sql = (
                    f" order by {_identifier(column, 'order column')}"
                    f" {'desc' if direction == 'desc' else 'asc'}"
                )
                continue
            if key == "limit":
                limit_sql = f" limit {int(raw)}"
                continue
            op, _, value = raw.partition(".")
            if op not in ("eq", "gt"):
                raise ValueError(f"filter operator {op!r} not supported by the fake")
            name = _identifier(key, "filter column")
            bound[name] = int(value) if value.lstrip("-").isdigit() else value
            clauses.append(f"{name} {'=' if op == 'eq' else '>'} :{name}")
        where = f" where {' and '.join(clauses)}" if clauses else ""
        sql = f"select * from {table}{where}{order_sql}{limit_sql}"  # noqa: S608 - identifiers allowlisted
        with sql_client.session() as conn:
            rows = [dict(row) for row in conn.execute(text(sql), bound).mappings()]
        return self._json(200, rows)

    # ------------------------------------------------------------------ POST

    def _insert(
        self,
        sql_client: TamperedClient,
        table: str,
        params: dict[str, str],
        request: httpx.Request,
    ) -> httpx.Response:
        body = json.loads(request.content)
        rows: list[dict[str, Any]] = body if isinstance(body, list) else [body]
        if not rows:
            return self._json(201, [])

        if "columns" in params:
            columns = [_identifier(c, "insert column") for c in params["columns"].split(",")]
        else:
            columns = [_identifier(c, "insert column") for c in rows[0]]

        prefer = request.headers.get("Prefer", "")
        conflict_sql = ""
        if "resolution=ignore-duplicates" in prefer:
            target = ", ".join(
                _identifier(c.strip(), "on_conflict column")
                for c in params.get("on_conflict", "").split(",")
                if c.strip()
            )
            conflict_sql = f" on conflict ({target}) do nothing" if target else ""
        returning_sql = " returning *" if "return=representation" in prefer else ""

        tuples: list[str] = []
        bound: dict[str, Any] = {}
        for index, row in enumerate(rows):
            placeholders: list[str] = []
            for column in columns:
                name = f"r{index}_{column}"
                value = row.get(column)
                if isinstance(value, dict | list):
                    bound[name] = json.dumps(value)
                    placeholders.append(f"cast(:{name} as json)")
                else:
                    bound[name] = value
                    placeholders.append(f":{name}")
            tuples.append(f"({', '.join(placeholders)})")

        sql = (  # noqa: S608 - identifiers allowlisted
            f"insert into {table} ({', '.join(columns)}) values {', '.join(tuples)}"
            f"{conflict_sql}{returning_sql}"
        )
        with sql_client.session() as conn:
            result = conn.execute(text(sql), bound)
            inserted = [dict(row) for row in result.mappings()] if returning_sql else []
            conn.commit()
        return self._json(201, inserted)

    # ---------------------------------------------------------------- errors

    @staticmethod
    def _json(status: int, payload: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(
            status,
            content=json.dumps(payload, default=_json_default),
            headers={"Content-Type": "application/json"},
        )

    @staticmethod
    def _error(status: int, code: str | None, message: str) -> httpx.Response:
        return httpx.Response(
            status,
            json={"code": code, "message": message, "details": None, "hint": None},
        )

    def _pg_error(self, exc: DBAPIError) -> httpx.Response:
        sqlstate = getattr(exc.orig, "sqlstate", None) or ""
        status = _SQLSTATE_STATUS.get(sqlstate, 400)
        return self._error(status, sqlstate or None, str(exc.orig))
