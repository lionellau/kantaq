"""Identity and capability grants (MOD-06 / Epic E06), v0.0.5 slice.

Three layers, per the architecture: human (Supabase Auth, team mode — E24),
device (Ed25519 keypair — v0.1), capability grant (signed permission — v0.1).
This package ships the v0.0.5 base: the role model (PRD §11), per-member
hashed bearer tokens for the loopback runtime, the verification cache that
keeps revocation under 5 seconds (NFR-E06-2), and the member lifecycle
(bootstrap / invite / list / revoke / rotate).
"""

from kantaq_core.identity.devices import (
    DEVICE_KEY_NAME,
    DeviceNotFoundError,
    device_private_key,
    ensure_device,
    local_device,
    revoke_device,
    verification_roots,
)
from kantaq_core.identity.grants import (
    DEFAULT_GRANT_TTL_SECONDS,
    MAX_AGENT_GRANT_TTL_SECONDS,
    MAX_GRANT_TTL_SECONDS,
    MAX_HUMAN_GRANT_TTL_SECONDS,
    GrantDeniedError,
    GrantNotFoundError,
    GrantService,
    ensure_member_grant,
    local_grant_index,
    max_grant_ttl_seconds,
    revoke_grants_for_device,
    revoke_grants_for_member,
    verify_grant_row,
)
from kantaq_core.identity.keychain import FileKeychain, Keychain
from kantaq_core.identity.roles import ROLE_PERMISSIONS, Action, Role, can
from kantaq_core.identity.service import (
    IdentityError,
    IdentityService,
    LastOwnerError,
    MemberNotFoundError,
    MintedToken,
)
from kantaq_core.identity.tokens import (
    TokenVerifier,
    VerifiedActor,
    hash_secret,
    mint_token,
    parse_token,
    verify_secret,
)

__all__ = [
    "DEFAULT_GRANT_TTL_SECONDS",
    "DEVICE_KEY_NAME",
    "MAX_AGENT_GRANT_TTL_SECONDS",
    "MAX_GRANT_TTL_SECONDS",
    "MAX_HUMAN_GRANT_TTL_SECONDS",
    "ROLE_PERMISSIONS",
    "Action",
    "DeviceNotFoundError",
    "FileKeychain",
    "GrantDeniedError",
    "GrantNotFoundError",
    "GrantService",
    "IdentityError",
    "IdentityService",
    "Keychain",
    "LastOwnerError",
    "MemberNotFoundError",
    "MintedToken",
    "Role",
    "TokenVerifier",
    "device_private_key",
    "ensure_device",
    "ensure_member_grant",
    "local_device",
    "local_grant_index",
    "max_grant_ttl_seconds",
    "revoke_device",
    "revoke_grants_for_device",
    "revoke_grants_for_member",
    "verify_grant_row",
    "verification_roots",
    "VerifiedActor",
    "can",
    "hash_secret",
    "mint_token",
    "parse_token",
    "verify_secret",
]
