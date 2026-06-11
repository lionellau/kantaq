"""kantaq Supabase backend adapter (MOD-05 / Epic E24).

v0.0.5 scope: the SQL artifacts (schema migration generated from the one
SQLModel metadata, hand-written RLS policies) and the Auth client (email magic
link). The sync endpoints (E24-T4) ride on the event log and land in Sprint 2;
the MOD-04 backend port is implemented then.
"""

from __future__ import annotations

from kantaq_backend_supabase.auth import (
    AuthError,
    Session,
    SupabaseAuth,
    User,
)
from kantaq_backend_supabase.keys import ServiceRoleKeyError, assert_client_safe_key, key_role
from kantaq_backend_supabase.schema import (
    COLLECTIONS_MIGRATION,
    POLICIES_FILE,
    generate_collections_sql,
    read_repo_sql,
)

__version__: str = "0.0.5"

__all__ = [
    "COLLECTIONS_MIGRATION",
    "POLICIES_FILE",
    "AuthError",
    "ServiceRoleKeyError",
    "Session",
    "SupabaseAuth",
    "User",
    "__version__",
    "assert_client_safe_key",
    "generate_collections_sql",
    "key_role",
    "read_repo_sql",
]
