"""kantaq self-hosted Postgres backend (MOD-28 / Epic E25).

A team runs the whole sync backend from one ``docker compose up`` — a
single-binary sync-server + Postgres — behaving identically to the Supabase
backend, because it reuses the **same validator core** (no fork, D-30):
``verify_event`` (grant + signature) and ``detect_merge`` (the §8.1 merge rule).

Public surface:

- ``PostgresSyncBackend`` — the ``BackendPort`` over one self-hosted Postgres.
- ``commit_events`` / ``to_commit_result`` — the atomic-commit core (the Python
  twin of ``supabase/rpc/events.sql``) and its typed-result mapper.
- ``create_schema`` — create the trust + collection tables and the ``sync_events``
  log on a fresh database.
- ``create_app`` — the FastAPI sync-server exposing the endpoint set.
"""

from __future__ import annotations

from kantaq_backend_postgres.backend import PostgresSyncBackend
from kantaq_backend_postgres.client import SyncBackendError, SyncServerBackend
from kantaq_backend_postgres.commit import commit_events, to_commit_result
from kantaq_backend_postgres.schema import create_schema

__all__ = [
    "PostgresSyncBackend",
    "SyncBackendError",
    "SyncServerBackend",
    "commit_events",
    "create_schema",
    "to_commit_result",
]


def __getattr__(name: str) -> object:
    # ``create_app`` lives in app.py, which imports FastAPI; keep the import lazy
    # so importing the backend core (commit/parity tests) doesn't require the
    # web stack.
    if name == "create_app":
        from kantaq_backend_postgres.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
