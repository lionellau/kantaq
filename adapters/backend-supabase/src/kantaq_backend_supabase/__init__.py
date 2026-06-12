"""kantaq Supabase backend adapter (MOD-05 / Epic E24).

v0.0.5 scope: the SQL artifacts (schema migration generated from the one
SQLModel metadata, hand-written RLS policies, the ``sync_events`` log), the
Auth client (email magic link), and the sync endpoints — ``SupabaseSyncBackend``
implements the MOD-04 backend port over PostgREST (E24-T4).
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
    SYNC_MIGRATION,
    SYNC_POLICIES_FILE,
    generate_collections_sql,
    read_repo_sql,
)
from kantaq_backend_supabase.sync import (
    PAGE_SIZE,
    SYNC_TABLE,
    SupabaseSyncBackend,
    SyncBackendError,
    SyncMember,
    lookup_active_members,
)

__version__: str = "0.0.5"

__all__ = [
    "COLLECTIONS_MIGRATION",
    "PAGE_SIZE",
    "POLICIES_FILE",
    "SYNC_MIGRATION",
    "SYNC_POLICIES_FILE",
    "SYNC_TABLE",
    "AuthError",
    "ServiceRoleKeyError",
    "Session",
    "SupabaseAuth",
    "SupabaseSyncBackend",
    "SyncBackendError",
    "SyncMember",
    "User",
    "__version__",
    "assert_client_safe_key",
    "generate_collections_sql",
    "key_role",
    "lookup_active_members",
    "read_repo_sql",
]
