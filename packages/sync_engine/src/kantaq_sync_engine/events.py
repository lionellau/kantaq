"""Protocol events and the backend port (MOD-04, architecture §6 / PRD §13.9).

``Event`` is the wire object: what the local log stores, what push submits,
what pull receives. As of E04-T4 it is **the** canonical ``kantaq_protocol``
``Event`` — re-exported here, not re-declared — so signing (``sign``/``verify``
over ``signing_bytes``) and the backend's verified ingestion (E24-T5) speak the
exact same nominal type and the exact same canonical bytes. One Event, one
codec, or signatures break (MOD-17). The MOD-30 harness re-exports this same
type, so ``FakeBackend`` accepts production events without adapters.

``BackendPort`` is the v0.0.5 cut of the §13.9 adapter contract — the three
calls online sync needs (submit, stream-since-cursor, snapshot). MOD-05
(Supabase) and MOD-28 (self-hosted) implement it; MOD-30's FakeBackend
satisfies it structurally.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from kantaq_protocol import Event, Op

__all__ = [
    "SYNC_VERSION",
    "BackendPort",
    "BackendUnavailable",
    "CommitResult",
    "CommittedEvent",
    "Event",
    "FieldConflict",
    "Op",
    "RebaseRequired",
    "SessionInit",
    "SyncVersionUnsupported",
    "fold_events",
]

# The sync wire/codec version (MOD-26 §B7 / DEBT-09). Bumped only when the event
# byte-shape or the commit protocol changes incompatibly; a replica tolerates a
# peer within ±1 of this (see SyncEngine.SYNC_VERSION_SKEW) so a staggered
# upgrade across a 2–10 person team interops during the rollout window.
SYNC_VERSION = 1


class SyncVersionUnsupported(Exception):
    """The backend's sync/schema version is too far from ours to interop safely.

    Raised by the handshake (MOD-26 §B7) **before** any drain or ingest, so the
    durable outbox and inbox are untouched — a future-version peer can never
    half-apply events an old replica would mis-decode, and a downgraded replica
    never strands its pending writes. Non-destructive by construction: the caller
    sees the raise, the local log is exactly as it was.
    """


class RebaseRequired(Exception):
    """A compare-and-swap (CAS) commit was refused because the entity's field
    head moved past the write's base (MOD-26 §B3/B4 / E05-T3).

    Raised by ``commit_events(..., cas=True)`` when a committed write WOULD
    contend with the committed head — used for conflict *resolutions* (a stale
    resolution must not land against a live newer contender) and approved agent
    *proposals* (an agent never silently lands a write whose base the team has
    moved past). Atomic: the RPC commits NOTHING for the call, so there is no
    half-applied value to undo and no partition window — the caller re-surfaces
    the conflict_record / flips the proposal to ``rebase_required`` and the
    pending write simply re-submits and re-rejects until the base is fresh.

    ``event`` is the offending write; ``conflicts`` carries the per-field detail
    (the contender the human re-decides against).
    """

    def __init__(self, event: Event, conflicts: tuple[FieldConflict, ...] = ()) -> None:
        super().__init__(f"event {event.event_id} needs rebase: its base is behind the field head")
        self.event = event
        self.conflicts = conflicts


class BackendUnavailable(Exception):
    """The backend could not be reached (transport / connectivity failure).

    The offline-aware flush loop (MOD-26 §B1) catches this to back off and
    retry; the events stay in the durable outbox and are re-attempted, so a
    partition never strands an offline write (NFR-E05-1). This is **not** a
    rejection — a rejection (bad signature, denied grant, stale base_rev) is a
    terminal per-event signal that takes the event *out* of the outbox. A
    backend raises this for a dropped connection or an unreachable host.
    """


@dataclass(frozen=True)
class CommittedEvent:
    """An event with the backend's assigned commit order (D-05)."""

    revision: int
    event: Event


@dataclass(frozen=True)
class FieldConflict:
    """One per-field conflict the atomic RPC detected (MOD-26 §B4, E05-T2).

    The **raw tuple** the committing client mints a ``conflict_record`` from: the
    contended field, the committed field-head revision in ``(base, head]`` it
    collides with, and both candidate scalars. The conflict-record id is hashed
    **client-side** from these (no plpgsql hash), so this carries only the raw
    inputs — keeping the merge id single-language (no cross-language drift).
    """

    field: str
    contending_revision: int
    head_value: Any
    incoming_value: Any


@dataclass(frozen=True)
class CommitResult:
    """One event's outcome from the v0.2 atomic commit RPC (E24-T6 / MOD-05).

    ``status`` is ``"committed"`` or ``"duplicate"`` (the dedup floor was hit on
    an idempotent re-push). ``revision`` is the assigned commit order.
    ``stale_base_rev`` is set (to the event's ``base_rev``) when that base was
    older than the committed head for the entity. ``conflicts`` is the per-field
    detail (E05-T2): when non-empty the committing client mints a signed
    ``conflict_record`` per entry. ``head_rev`` is the committed head observed
    before this event committed.
    """

    event_id: str
    status: str
    revision: int
    base_rev: int | None
    head_rev: int
    stale_base_rev: int | None
    conflicts: tuple[FieldConflict, ...] = ()

    @property
    def is_stale(self) -> bool:
        return self.stale_base_rev is not None


@dataclass(frozen=True)
class SessionInit:
    """The peer's advertised versions from the §B7 handshake.

    Exchanged once per session before any sync I/O: the client advertises its
    ``SYNC_VERSION`` + ``EXPECTED_SCHEMA_VERSION`` and the backend returns its
    own. The engine refuses to proceed if the peer is more than one version away
    (``SyncVersionUnsupported``), so a codec/schema mismatch is caught at the
    door rather than corrupting the durable log mid-stream.
    """

    sync_version: int
    schema_version: int


class BackendPort(Protocol):
    """What the sync engine needs from a backend (implemented by MOD-05/28)."""

    def session_init(self, *, sync_version: int, schema_version: int) -> SessionInit:
        """Exchange protocol versions before any sync I/O (MOD-26 §B7).

        The client advertises its ``sync_version`` + ``schema_version``; the
        backend records them and returns its own. Optional: a pre-handshake
        transport may omit it, in which case the engine treats the peer as
        same-version (negotiation skipped) for backward compatibility.
        """
        ...

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Commit new events in submission order; drop (actor_id, actor_seq)
        duplicates silently so a retry can never double-commit.

        v0.1 transport (raw PostgREST upsert). The v0.2 DEBT-25 cutover routes
        every write through ``commit_events`` (the atomic RPC) instead; ``push``
        stays for the convergence fixtures and pre-cutover history.
        """
        ...

    def commit_events(
        self, events: Iterable[Event], *, require_signature: bool = True, cas: bool = False
    ) -> list[CommitResult]:
        """Commit events through the v0.2 atomic RPC (E24-T6, D-09): one
        transaction validates grant + ordering, applies the merge policy,
        assigns the revision, and returns each event's structured outcome
        (including ``stale_base_rev``). The DEBT-25 cutover routes every write
        here. Client-side Ed25519 byte-verification stays at the
        ``VerifyingBackend`` edge; ``require_signature`` is the RPC's
        defense-in-depth presence check.

        ``cas`` (MOD-26 §B3/B4 / E05-T3) makes the call a compare-and-swap: if
        any committed write in it WOULD contend with the entity's committed field
        head, the RPC commits NOTHING and raises ``RebaseRequired`` (atomic).
        Used for conflict resolutions and approved agent proposals — writes that
        must not silently land against a moved head. Ordinary writes leave it
        ``False`` (commit-and-flag, ride-flagged).
        """
        ...

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        """Committed events with revision > ``since``, in commit order."""
        ...

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """The backend's fold of a collection (LWW by commit order)."""
        ...


def fold_events(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    """Fold an ordered event stream into per-entity state (D-05 LWW).

    The backend-side fold shape: ``patch`` overwrites fields last-writer-wins,
    ``append`` accumulates under ``_appended``, ``tombstone`` removes the
    entity. One fold, one truth — MOD-30's FakeBackend and the real backend
    adapters (MOD-05/28) all fold with this function, so the contract tests
    and production cannot drift apart.
    """
    state: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.op == "tombstone":
            state.pop(event.entity_id, None)
            continue
        current = state.setdefault(event.entity_id, {})
        if event.op == "append":
            current.setdefault("_appended", []).append(event.payload)
        else:  # patch: last writer wins on scalar fields
            current.update(event.payload)
    return state
