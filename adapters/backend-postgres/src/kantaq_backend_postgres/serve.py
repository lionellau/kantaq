"""The sync-server entrypoint (E25-T2 / MOD-28) — ``python -m`` + a uvicorn factory.

The container runs this: it reads the Postgres URL from the environment, creates
the schema (idempotent — safe on every boot), and serves ``create_app`` with
uvicorn. ``KANTAQ_DATABASE_URL`` is the connection string the docker-compose
wires to the ``postgres`` service; ``KANTAQ_SYNC_HOST`` / ``KANTAQ_SYNC_PORT``
bind the listener (defaults ``0.0.0.0:8889`` so it is reachable from the
compose network and the host, behind Caddy in production).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from kantaq_backend_postgres.app import create_app
from kantaq_backend_postgres.schema import create_schema

DATABASE_URL_ENV = "KANTAQ_DATABASE_URL"
HOST_ENV = "KANTAQ_SYNC_HOST"
PORT_ENV = "KANTAQ_SYNC_PORT"
REQUIRE_SIGNATURE_ENV = "KANTAQ_REQUIRE_SIGNATURE"


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _engine_from_env() -> Engine:
    raw = os.environ.get(DATABASE_URL_ENV)
    if not raw:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is required (e.g. postgresql://kantaq:...@postgres:5432/kantaq)"
        )
    # Force the psycopg (v3) driver so a bare postgresql:// URL works, matching
    # the rest of the stack (kantaq_test_harness.db._normalize_driver).
    url = make_url(raw)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def create_app_from_env() -> FastAPI:
    """uvicorn factory: build the schema-ready app from the environment.

    ``KANTAQ_REQUIRE_SIGNATURE=true`` is the post-cutover floor: the server then
    rejects every unsigned / grant-less write and clients cannot relax it (SEC).
    Default ``false`` matches a fresh, pre-cutover workspace.
    """
    engine = _engine_from_env()
    create_schema(engine)
    return create_app(engine, require_signature=_env_flag(REQUIRE_SIGNATURE_ENV, default=False))


def main() -> None:
    import uvicorn

    host = os.environ.get(HOST_ENV, "0.0.0.0")  # noqa: S104 - a networked team server, behind Caddy
    port = int(os.environ.get(PORT_ENV, "8889"))
    uvicorn.run(
        "kantaq_backend_postgres.serve:create_app_from_env",
        factory=True,
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
