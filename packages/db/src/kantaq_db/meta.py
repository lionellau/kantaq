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

# The 8 v0.0.5 collections (architecture §6) plus the v0.1 additions: the
# memory collections (E13 / MOD-19), the typed ticket relationships (E12 /
# MOD-03), and the identity pair (E06 / MOD-06). schema_version is
# infrastructure, not a collection, so it is deliberately absent here. The
# collection-level privacy class stays the default (team/plain/standard) for
# memory too: rows tighten to visibility="local" per entity (D-14 — tightened,
# never loosened).
COLLECTION_META: dict[str, CollectionMeta] = {
    "workspaces": CollectionMeta("workspaces", "backend", "lww", _DEFAULT_PRIVACY),
    "projects": CollectionMeta("projects", "backend", "lww", _DEFAULT_PRIVACY),
    "tickets": CollectionMeta("tickets", "backend", "lww", _DEFAULT_PRIVACY),
    "comments": CollectionMeta("comments", "backend", "append_only", _DEFAULT_PRIVACY),
    # E12 v0.1: typed ticket relationships — an edge is created and tombstoned,
    # never patched, so it converges lww like any backend collection.
    "ticket_relationships": CollectionMeta(
        "ticket_relationships", "backend", "lww", _DEFAULT_PRIVACY
    ),
    "members": CollectionMeta("members", "backend", "lww", _DEFAULT_PRIVACY),
    "tokens": CollectionMeta("tokens", "local", "authoritative_tx", _DEFAULT_PRIVACY),
    "audit_events": CollectionMeta("audit_events", "backend", "append_only", _DEFAULT_PRIVACY),
    "agent_proposals": CollectionMeta("agent_proposals", "backend", "lww", _DEFAULT_PRIVACY),
    "memory_entries": CollectionMeta("memory_entries", "backend", "lww", _DEFAULT_PRIVACY),
    "memory_links": CollectionMeta("memory_links", "backend", "lww", _DEFAULT_PRIVACY),
    # E06 v0.1: device verify keys sync like any collection (teammates need
    # each other's roots); grants are authoritative_tx — never optimistic.
    "devices": CollectionMeta("devices", "backend", "lww", _DEFAULT_PRIVACY),
    "capability_grants": CollectionMeta(
        "capability_grants", "backend", "authoritative_tx", _DEFAULT_PRIVACY
    ),
    # E17 v0.2 (MOD-22): the db-backed skill registry. Full table treatment
    # (model/migration/parity/Supabase DDL/RLS) but OFF the sync allowlist in
    # v0.2 — architecture §6.1 lists them as "backend registry"; cross-replica
    # registry sync is deferred, so CRUD writes locally + audited, never emitted.
    "skill_containers": CollectionMeta("skill_containers", "backend", "lww", _DEFAULT_PRIVACY),
    "skill_mappings": CollectionMeta("skill_mappings", "backend", "lww", _DEFAULT_PRIVACY),
}


def collection_names() -> tuple[str, ...]:
    """The declared collection names, in declaration order (15 in v0.2)."""
    return tuple(COLLECTION_META)
