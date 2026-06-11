"""Per-collection protocol metadata (FR-E02-3).

Each v0.0.5 collection declares three things the protocol/sync layers (E03/E04)
will consume:

- ``authority_mode`` — who is authoritative for a committed value. In v0.0.5 the
  sync backend assigns commit order (last-writer-wins by server order, D-05), so
  syncable collections are ``backend``-authoritative; local-only material
  (bearer tokens) is ``local``.
- ``merge_policy`` — how concurrent writes combine: ``lww`` (last-writer-wins
  scalars), ``append_only`` (logs that never conflict), ``authoritative_tx``
  (never written optimistically — grants/tokens), or the ``crdt`` stub (unused
  in MVP).
- ``privacy_class`` — ``visibility`` / ``hosting_mode`` / ``retention_policy``.
  All three columns exist on every row, but MVP only *uses* the subset
  ``visibility ∈ {local, team}`` / ``hosting_mode = plain`` /
  ``retention_policy = standard`` (D-14). The richer values are schema-present
  and unused until v0.2+ (DEBT-11).

This module is data only; it imports nothing from the ORM so the sync layer can
read it without pulling in SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AuthorityMode = Literal["local", "backend"]
MergePolicy = Literal["lww", "append_only", "authoritative_tx", "crdt"]
Visibility = Literal["local", "team"]
HostingMode = Literal["plain"]
RetentionPolicy = Literal["standard"]


@dataclass(frozen=True)
class PrivacyClass:
    """The three privacy dimensions (PRD §6.10); MVP uses the subset below."""

    visibility: Visibility = "team"
    hosting_mode: HostingMode = "plain"
    retention_policy: RetentionPolicy = "standard"


@dataclass(frozen=True)
class CollectionMeta:
    name: str
    authority_mode: AuthorityMode
    merge_policy: MergePolicy
    privacy_class: PrivacyClass


_DEFAULT_PRIVACY = PrivacyClass()

# The 8 v0.0.5 collections (architecture §6). schema_version is infrastructure,
# not a collection, so it is deliberately absent here.
COLLECTION_META: dict[str, CollectionMeta] = {
    "workspaces": CollectionMeta("workspaces", "backend", "lww", _DEFAULT_PRIVACY),
    "projects": CollectionMeta("projects", "backend", "lww", _DEFAULT_PRIVACY),
    "tickets": CollectionMeta("tickets", "backend", "lww", _DEFAULT_PRIVACY),
    "comments": CollectionMeta("comments", "backend", "append_only", _DEFAULT_PRIVACY),
    "members": CollectionMeta("members", "backend", "lww", _DEFAULT_PRIVACY),
    "tokens": CollectionMeta("tokens", "local", "authoritative_tx", _DEFAULT_PRIVACY),
    "audit_events": CollectionMeta("audit_events", "backend", "append_only", _DEFAULT_PRIVACY),
    "agent_proposals": CollectionMeta("agent_proposals", "backend", "lww", _DEFAULT_PRIVACY),
}


def collection_names() -> tuple[str, ...]:
    """The 8 syncable collection names, in declaration order."""
    return tuple(COLLECTION_META)
