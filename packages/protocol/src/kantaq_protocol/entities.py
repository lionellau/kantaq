"""Protocol entity types (FR-E03-1, architecture §6).

Frozen dataclasses — pure values, no ORM, no I/O — so every layer (sync
engine, backends, gateway) can pass them around without importing storage.
``Event`` is field-for-field the wire shape the sync engine and the MOD-30
FakeBackend already speak (MOD-04 adopts this module's codec in Sprint 4;
nothing reshapes).

Timestamps that participate in signatures (grant validity) are **integer
unix seconds, UTC**: the canonical codec (RFC 8785 restricted profile,
``canonical.py``) carries no floats and no datetime formatting ambiguity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Op = Literal["patch", "append", "tombstone"]
OPS: tuple[Op, ...] = ("patch", "append", "tombstone")

ActorKind = Literal["human", "device", "agent"]

# Architecture §8.1 merge policies; ``crdt`` is a stub until post-v0.1.
MergePolicy = Literal["lww", "append_only", "authoritative_tx", "crdt"]


@dataclass(frozen=True, slots=True)
class Actor:
    """A protocol actor: a human member, a device runtime, or an agent.

    ``public_key`` is the hex Ed25519 verify key for ``device`` actors (D-01:
    only the local runtime signs); humans and agents act *through* a device
    and carry no key of their own.
    """

    actor_id: str
    kind: ActorKind
    public_key: str | None = None
    label: str = ""


@dataclass(frozen=True, slots=True)
class Collection:
    """A collection declaration (FR-E02-3): the sync layer's contract."""

    name: str
    authority_mode: Literal["local", "backend"]
    merge_policy: MergePolicy
    visibility: Literal["local", "team"] = "team"
    hosting_mode: str = "plain"
    retention_policy: str = "standard"


@dataclass(frozen=True, slots=True)
class TeamManifest:
    """The workspace's protocol self-description: who signs, what syncs."""

    team_id: str
    name: str
    actors: tuple[Actor, ...] = ()
    collections: tuple[Collection, ...] = ()


@dataclass(frozen=True, slots=True)
class Event:
    """One protocol event — the unit everything signs, stores, and syncs."""

    event_id: str
    collection: str
    entity_id: str
    actor_id: str
    actor_seq: int
    op: Op = "patch"
    base_rev: int | None = None
    policy_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sig: str | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A backend fold of one collection at a revision (MOD-04 pull bootstrap)."""

    collection: str
    as_of_rev: int
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """A signed permission slip (PRD §6.9): who may do what, for how long.

    ``subject`` is the member/agent actor the grant empowers; ``issuer`` is
    the **device** that signed it (its public key must be a known root for
    verification). ``verbs`` are the granted actions on ``resource``.
    ``issued_at``/``expires_at`` are unix seconds UTC. ``revokes`` optionally
    names a prior grant this one replaces (rotation). Grants merge as
    ``authoritative_tx`` — never written optimistically (MOD-06).
    """

    grant_id: str
    subject: str
    issuer: str
    resource: str
    verbs: tuple[str, ...]
    issued_at: int
    expires_at: int
    revokes: str | None = None
    sig: str | None = None


@dataclass(frozen=True, slots=True)
class BlobRef:
    """A content-addressed reference to bytes stored outside the event log."""

    blob_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AuditAnchor:
    """A periodic hash anchor over an audit range (E07; populated in v0.2)."""

    anchor_id: str
    range_start: str
    range_end: str
    chain_hash: str
