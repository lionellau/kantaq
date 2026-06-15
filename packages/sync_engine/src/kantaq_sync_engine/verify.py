"""Verified ingestion: the backend refuses what it cannot verify (E24-T5).

The signing half (the ``EventLogSink``, E04-T4) is paired here with the verify
half. ``verify_event`` is the whole check, in one fail-closed function:

1. the event is signed (``unsigned`` if not, post-cutover);
2. its ``policy_ref`` resolves to a grant we hold (``policy_denied`` if not);
3. the grant verifies against the device roots — signature, validity window,
   revocation (``policy_denied`` with the grant's own reason if not);
4. the grant's ``subject`` is the event's ``actor_id`` (``policy_denied``);
5. the event's signature verifies against the grant's **issuing device** key
   (``invalid_signature`` if a byte was changed).

``VerifyingBackend`` applies it at the sync boundary: it refuses to **push** an
event it cannot verify (atomic — nothing is submitted) and refuses to **fold**
one on **pull** (fail closed — an unverifiable remote event is dropped, never
applied), each via the structured codes below and an optional denial hook.

**Where v0.1 stops, stated plainly.** The reject vocabulary is FR-E03-5's; this
layer emits ``unsigned`` / ``invalid_signature`` / ``policy_denied`` (and
``schema_violation`` for a malformed event). ``stale_base_rev`` is in the
vocabulary but **not emitted** — v0.0.5/v0.1 resolve conflicts by server commit
order (LWW, D-05), so a stale ``base_rev`` is folded, not rejected; optimistic
concurrency is the v0.2 atomic-RPC concern (D-09). The grant's **validity
window is enforced at ingest** (``require_signature``/``now``); a runtime that
pulls events older than a peer's 24 h grant would re-derive nothing new at
human-scale poll rates, and re-verifying long-committed history against a
fresh clock is the v0.2 server-RPC's job — not claimed here.

There is **no Ed25519 in Postgres** in v0.0.5/v0.1, so this is enforced where
the trust store lives (the runtime, on push and on pull) rather than inside the
database; the server-side reject moves into the atomic plpgsql RPC at v0.2
(FR-E24-3, D-09). Until then "the backend refuses" means *no conformant peer
accepts an event it cannot verify* — which is the security boundary signing
buys over RLS alone.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from kantaq_protocol import CapabilityGrant, SchemaViolation, verify, verify_grant
from kantaq_sync_engine.events import (
    BackendPort,
    CommitResult,
    CommittedEvent,
    Event,
    fold_events,
)

# Reject codes — FR-E03-5's wire vocabulary, extended for the two event-level
# signature failures the grant vocabulary does not name.
VERIFY_OK = "ok"
UNSIGNED = "unsigned"
INVALID_SIGNATURE = "invalid_signature"
POLICY_DENIED = "policy_denied"
SCHEMA_VIOLATION = "schema_violation"
STALE_BASE_REV = "stale_base_rev"  # reserved (v0.2 merge policy); never emitted in v0.1

# The grant verbs that authorise a write to each syncable collection — the
# fine, per-verb scoping D-03 assigns to the grants layer (RLS stays coarse).
# Any one listed verb suffices (a human approver decides a proposal with
# ``tickets.write``; an agent proposes with ``proposals.write``). The strings
# are the PRD §11 ``Action`` vocabulary (kantaq_core.identity.roles); keep this
# aligned as more surfaces adopt signed writes. A collection absent here is not
# verb-checked — the syncable allowlist (the DB CHECK + SYNCABLE_MODELS) bounds
# the collection set.
_COLLECTION_WRITE_VERBS: dict[str, frozenset[str]] = {
    "workspaces": frozenset({"tickets.write", "members.invite"}),
    "projects": frozenset({"tickets.write"}),
    "tickets": frozenset({"tickets.write"}),
    "comments": frozenset({"tickets.write"}),
    "ticket_relationships": frozenset({"tickets.write"}),
    "members": frozenset({"members.invite", "members.revoke"}),
    "agent_proposals": frozenset({"proposals.write", "tickets.write"}),
    "memory_entries": frozenset({"memory.write"}),
    "memory_links": frozenset({"memory.write"}),
}


@dataclass(frozen=True)
class EventVerification:
    """A structured verdict — never a bare bool (mirrors ``GrantVerification``)."""

    ok: bool
    code: str
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class VerifyContext:
    """The trust store a verification reads against, at one instant.

    ``roots`` maps a device id to its Ed25519 verify key; ``grants`` maps a
    grant id to the signed capability grant; ``now`` is unix seconds for the
    grant window; ``revoked_ids`` are the grant ids the store knows are revoked
    (a signature cannot prove an absence). ``require_signature`` is the cutover
    state: ``True`` once a workspace has cut over to signed sync. ``workspace_id``
    is the ingestion workspace: when set, the grant's ``resource`` must scope it,
    so the gate enforces workspace scope itself rather than leaning entirely on
    RLS (DEBT-15(d) / E27 HIGH-2(b)). ``None`` skips the check (single-store tests).
    """

    roots: Mapping[str, str]
    grants: Mapping[str, CapabilityGrant]
    now: int
    revoked_ids: Collection[str] = ()
    require_signature: bool = True
    workspace_id: str | None = None


def verify_event(event: Event, context: VerifyContext) -> EventVerification:
    """Verify one event against the trust store. Fail closed, structured code.

    Total: it always returns a verdict, never raises. A malformed (untrusted)
    event whose canonical re-encoding fails is a ``schema_violation`` drop, not
    an exception that would crash the caller's pull loop on one poisoned event.
    """
    if event.sig is None:
        if context.require_signature:
            return EventVerification(False, UNSIGNED, "event carries no signature")
        return EventVerification(True, VERIFY_OK, "unsigned (pre-cutover)")

    try:
        grant = context.grants.get(event.policy_ref) if event.policy_ref else None
        if grant is None:
            return EventVerification(False, POLICY_DENIED, "policy_ref names no grant we hold")

        grant_check = verify_grant(
            grant, context.roots, now=context.now, revoked_ids=context.revoked_ids
        )
        if not grant_check.ok:
            return EventVerification(False, POLICY_DENIED, f"grant {grant_check.reason}")

        if grant.subject != event.actor_id:
            return EventVerification(
                False, POLICY_DENIED, "grant subject does not authorise this actor"
            )

        # Workspace scope (DEBT-15(d)): the grant must be issued for the
        # workspace we are ingesting into — the gate enforces this itself rather
        # than leaning entirely on RLS, closing the resource axis before
        # multi-workspace sync (E27 HIGH-2(b)).
        if context.workspace_id is not None and grant.resource != context.workspace_id:
            return EventVerification(
                False, POLICY_DENIED, "grant resource does not scope this workspace"
            )

        # Fine per-verb scoping (D-03): the grant must carry a verb that
        # authorises this collection. The full-role self-grants pass; a narrow
        # grant (e.g. an agent scoped to proposals) cannot ride a ticket write.
        acceptable = _COLLECTION_WRITE_VERBS.get(event.collection)
        if acceptable is not None and acceptable.isdisjoint(grant.verbs):
            return EventVerification(
                False, POLICY_DENIED, f"grant does not authorise writes to {event.collection!r}"
            )

        # The grant verified against roots, so its issuer is a known device root.
        if not verify(event, context.roots[grant.issuer]):
            return EventVerification(False, INVALID_SIGNATURE, "event signature does not verify")
    except SchemaViolation as exc:
        # ``verify_grant``/``signing_bytes`` re-validate their inputs and refuse
        # a non-canonical one — surface it as a drop, never a crash.
        return EventVerification(False, SCHEMA_VIOLATION, f"not canonically encodable: {exc}")

    return EventVerification(True, VERIFY_OK)


class EventRejected(Exception):
    """A push carried an event that failed verification (atomic — none committed)."""

    def __init__(self, verification: EventVerification, event: Event) -> None:
        super().__init__(
            f"event {event.event_id} rejected: {verification.code} ({verification.reason})"
        )
        self.code = verification.code
        self.reason = verification.reason
        self.event = event


@dataclass
class VerifyingBackend:
    """A ``BackendPort`` decorator that verifies signature + grant at ingest.

    ``context`` is called per operation so each push/pull reads the trust store
    fresh (new devices, new or revoked grants). Events at or below
    ``cutover_rev`` are pre-cutover, unsigned-immutable history and are passed
    through unverified. ``on_deny`` (optional) is invoked for each rejected
    event — the runtime writes the NFR-E09-1 denial audit row there.
    """

    inner: BackendPort
    context: Callable[[], VerifyContext]
    cutover_rev: int = 0
    on_deny: Callable[[Event, EventVerification], None] | None = field(default=None)

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Verify every event before submitting; reject the batch if any fail."""
        batch = list(events)
        ctx = self.context()
        for event in batch:
            verdict = verify_event(event, ctx)
            if not verdict.ok:
                self._deny(event, verdict)
                raise EventRejected(verdict, event)
        return self.inner.push(batch)

    def commit_events(
        self, events: Iterable[Event], *, require_signature: bool = True
    ) -> list[CommitResult]:
        """The DEBT-25 commit path: verify every event (the authoritative
        client-side Ed25519 wall — the RPC cannot check the bytes, D-09) before
        committing; reject the batch atomically if any fail, then delegate to
        the atomic RPC."""
        batch = list(events)
        ctx = self.context()
        for event in batch:
            verdict = verify_event(event, ctx)
            if not verdict.ok:
                self._deny(event, verdict)
                raise EventRejected(verdict, event)
        return self.inner.commit_events(batch, require_signature=require_signature)

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        """Return only the committed events that verify; drop + audit the rest."""
        ctx = self.context()
        kept: list[CommittedEvent] = []
        for entry in self.inner.pull(collection, since):
            if entry.revision <= self.cutover_rev:
                kept.append(entry)  # pre-cutover, unsigned-immutable history
                continue
            verdict = verify_event(entry.event, ctx)
            if not verdict.ok:
                self._deny(entry.event, verdict)
                continue  # fail closed: never fold an unverifiable event
            kept.append(entry)
        return kept

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """The fold of the verified pull (unverifiable events never appear)."""
        return fold_events(entry.event for entry in self.pull(collection))

    def _deny(self, event: Event, verdict: EventVerification) -> None:
        if self.on_deny is not None:
            self.on_deny(event, verdict)
