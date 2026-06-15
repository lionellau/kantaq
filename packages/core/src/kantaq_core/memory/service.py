"""Memory domain service (MOD-19 / E13): entries, links, the privacy boundary.

The one write path for memory state, mirroring the tracker service contract:
every mutation validates first, applies the optimistic local write, writes an
attributed audit row (MOD-07), and emits a ``DomainEvent`` to the sink — with
one deliberate exception that *is* this module's security property:

**``visibility=local`` rows never reach the sink (NFR-E13-1).** Enforcement
lives at the emit seam, not in a downstream sync filter: private content never
enters ``event_log``, so no push path — present or future — can carry it off
the machine. Events are emitted only for the explicit ``visibility="team"``
allowlist, so an unknown visibility value fails closed (stays local).

Two corollaries keep the boundary tight:

- ``visibility`` is immutable after create. Loosening ``local→team`` is the
  v0.2 human-gated promotion workflow; tightening ``team→local`` would strand
  already-synced copies on the backend.
- A link inherits its entry's visibility, and audit rows for local rows carry
  no content snapshots; a link to a local entry is audited on the *memory
  entry* (not the ticket), so the private association never enters the
  ticket's activity feed or the syncable audit collection.

Timestamps are injectable (``now=``) so tests drive them with FakeClock; the
same clock decides ``expires_at`` filtering.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_core.tracker.events import DomainEvent, EventSink, Op
from kantaq_db.models import MemoryEntry, MemoryLink, Ticket

# The pinned v0.1 vocabularies (MOD-19 spec, "Data"). Stored as VARCHARs for
# dialect parity (D-07); validated here, the one write path.
MEMORY_TYPES: tuple[str, ...] = ("note", "decision", "constraint", "learning", "reference")
MEMORY_SOURCES: tuple[str, ...] = ("manual", "agent", "import")
MEMORY_SPACES: tuple[str, ...] = (
    "workspace",
    "project",
    "ticket",
    "codebase",
    "decision",
    "release",
    "agent_run",
)
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
REVIEW_STATUSES: tuple[str, ...] = ("draft", "proposed", "approved", "stale", "rejected")
# v0.1 writes may only set these; proposed/approved/rejected belong to the
# v0.2 human-gated promotion workflow (FR-E13-3).
WRITABLE_REVIEW_STATUSES: tuple[str, ...] = ("draft", "stale")
MEMORY_VISIBILITIES: tuple[str, ...] = ("local", "team")

# Keys a provenance dict may carry ({origin, actor_id, captured_at, detail}).
_PROVENANCE_KEYS = frozenset({"origin", "actor_id", "captured_at", "detail"})

# Loose "collection/id" refs for linked_entities (the typed ticket links live
# in memory_links).
_ENTITY_REF = re.compile(r"[a-z_]{1,32}/[A-Za-z0-9_-]{1,64}")

_TITLE_MAX = 500
_REASON_MAX = 500
_BODY_MAX = 100_000

_PATCHABLE = frozenset(
    {
        "title",
        "body",
        "type",
        "source",
        "space",
        "linked_entities",
        "provenance",
        "confidence",
        "review_status",
        "expires_at",
    }
)


def domain_visibility(visibility: str, review_status: str, space: str) -> str:
    """The UX-facing visibility label (PRD §8.8) — the one mapping table.

    The protocol stores ``visibility`` + ``review_status``; the UI speaks
    ``private_local`` / ``personal_synced`` / …. Keeping the mapping here (and
    nowhere else) is the MOD-19 "single source of truth" rule.
    """
    if visibility == "local":
        if space == "agent_run" and review_status == "draft":
            return "agent_run_private"
        return "private_local"
    if review_status == "proposed":
        return "proposal_context"
    if review_status == "approved":
        return "shared_workspace"
    return "personal_synced"


def _default_now() -> datetime:
    return datetime.now(UTC)


def _naive_utc(ts: datetime) -> datetime:
    """UTC wall time without tzinfo — the store's (and the fold's) encoding."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class MemoryGraphError(Exception):
    """Base class for memory domain errors."""


class MemoryValidationError(MemoryGraphError):
    """The request was understood but violates a domain rule (HTTP 422)."""


class MemoryNotFoundError(MemoryGraphError):
    def __init__(self, collection: str, entity_id: str) -> None:
        super().__init__(f"no such {collection.rstrip('s').replace('_', ' ')}: {entity_id}")
        self.collection = collection
        self.entity_id = entity_id


class MemoryConflictError(MemoryGraphError):
    """A compare-and-swap on ``review_status`` lost the race (HTTP 409).

    The row was decided concurrently or is no longer ``proposed`` — mirrors
    ``proposals.ProposalConflictError`` so the two human-gated decision paths
    share one double-apply guard.
    """


class MemoryService:
    """Memory CRUD + links bound to one acting member and one session."""

    def __init__(
        self,
        session: Session,
        *,
        actor_id: str,
        source: str = "app",
        sink: EventSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._source = source
        self._sink = sink
        self._raw_now: Callable[[], datetime] = now or _default_now

    def _now(self) -> datetime:
        return _naive_utc(self._raw_now())

    # ------------------------------------------------------------------ events

    def _emit_team_only(
        self, collection: str, row: MemoryEntry | MemoryLink, op: Op, payload: dict[str, Any]
    ) -> None:
        """Emit to the sink **only** for the explicit ``team`` allowlist.

        This single conditional is NFR-E13-1: a ``local`` row produces no
        event, so nothing about it ever enters the event log or any push.
        Allowlist, not denylist — an unexpected visibility value stays local.
        """
        if row.visibility != "team":
            return
        if self._sink is not None:
            self._sink.emit(
                DomainEvent(collection=collection, entity_id=row.id, op=op, payload=payload)
            )

    # ----------------------------------------------------------------- entries

    def create_entry(
        self,
        *,
        title: str,
        body: str = "",
        type: str = "note",  # noqa: A002 — mirrors the model field
        source: str = "manual",
        space: str = "workspace",
        visibility: str = "team",
        confidence: str = "medium",
        linked_entities: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        if visibility not in MEMORY_VISIBILITIES:
            raise MemoryValidationError(
                f"unknown visibility {visibility!r}; expected one of {MEMORY_VISIBILITIES}"
            )
        fields = self._validated_fields(
            {
                "title": title,
                "body": body,
                "type": type,
                "source": source,
                "space": space,
                "confidence": confidence,
                "linked_entities": list(linked_entities) if linked_entities is not None else [],
                "provenance": dict(provenance) if provenance is not None else {},
                "expires_at": expires_at,
            }
        )

        ts = self._now()
        # Provenance defaults: who/when/how (PRD §15), completed server-side so
        # every entry carries them even when the caller sends nothing.
        prov = fields["provenance"]
        prov.setdefault("origin", fields["source"])
        prov.setdefault("actor_id", self._actor_id)
        prov.setdefault("captured_at", ts.isoformat())

        entry = MemoryEntry(
            created_by=self._actor_id,
            visibility=visibility,
            created_at=ts,
            updated_at=ts,
            **fields,
        )
        self._session.add(entry)
        self._session.flush()
        self._audit_entry_write("memory.create", entry, before=None, now=ts)
        self._emit_team_only("memory_entries", entry, "patch", audit.snapshot(entry))
        self._session.commit()
        self._session.refresh(entry)
        return entry

    def update_entry(self, memory_id: str, changes: dict[str, Any]) -> MemoryEntry:
        entry = self.get_entry(memory_id)
        if "visibility" in changes:
            # Immutable in v0.1: loosening is the v0.2 promotion workflow;
            # tightening would strand already-synced copies (MOD-19 rule).
            raise MemoryValidationError(
                "visibility is immutable; promotion (v0.2) is the path to share an entry"
            )
        unknown = set(changes) - _PATCHABLE
        if unknown:
            raise MemoryValidationError(f"unknown memory fields: {sorted(unknown)}")
        validated = self._validated_fields(changes)
        if "review_status" in validated and validated["review_status"] not in (
            WRITABLE_REVIEW_STATUSES
        ):
            raise MemoryValidationError(
                f"review_status writes are limited to {WRITABLE_REVIEW_STATUSES} in v0.1; "
                "proposed/approved/rejected arrive with the promotion workflow"
            )

        before = audit.snapshot(entry) if entry.visibility == "team" else None
        ts = self._now()
        for fieldname, value in validated.items():
            setattr(entry, fieldname, value)
        entry.updated_at = ts
        self._session.add(entry)
        self._session.flush()
        self._audit_entry_write("memory.update", entry, before=before, now=ts)
        after = audit.snapshot(entry)
        patch_payload = {key: after[key] for key in validated}
        patch_payload["updated_at"] = after["updated_at"]
        self._emit_team_only("memory_entries", entry, "patch", patch_payload)
        self._session.commit()
        self._session.refresh(entry)
        return entry

    # ------------------------------------------------------- promotion (E13-T4)

    # Copyable content for a local→team promotion (MOD-19 §52): the substance of
    # the note, never its envelope (id/visibility/review_status/audit fields).
    _PROMOTABLE_FIELDS: tuple[str, ...] = (
        "title",
        "body",
        "type",
        "space",
        "confidence",
        "linked_entities",
        "expires_at",
    )

    def promote(self, memory_id: str) -> MemoryEntry:
        """Propose an entry into the shared collection (the PROPOSE step).

        This is the agent-reachable half (``memory.write``); approve/reject is
        human-only. The copy-on-promote model (MOD-19 §52 + sprint-6 §71 +
        exit-criterion 5) keeps ``visibility=local`` immutable and never-syncing:

        * A ``local`` source is **never mutated**. A NEW ``team`` row is created
          at ``review_status="proposed"`` carrying the source's content; the
          original keeps ``visibility="local"`` and its ``review_status``, and
          emits nothing (NFR-E13-1 re-proven across promote). The new row's
          ``provenance`` records the source id.
        * A ``team`` row in ``{draft, stale}`` transitions **in place** to
          ``proposed`` (no copy).
        * Any other ``team`` state (already proposed/approved/rejected) is
          rejected — only approve/reject moves those.
        """
        entry = self.get_entry(memory_id)
        ts = self._now()
        if entry.visibility == "local":
            return self._promote_local_copy(entry, now=ts)
        if entry.review_status not in WRITABLE_REVIEW_STATUSES:
            raise MemoryValidationError(
                f"a team entry in review_status {entry.review_status!r} cannot be promoted; "
                f"only {WRITABLE_REVIEW_STATUSES} entries may be proposed "
                "(approve/reject moves proposed/approved/rejected rows)"
            )
        return self._promote_team_in_place(entry, now=ts)

    def _promote_local_copy(self, source: MemoryEntry, *, now: datetime) -> MemoryEntry:
        """Copy a ``local`` source into a NEW ``team`` ``proposed`` row.

        The source is left untouched — no write, no audit-after, no event — so
        ``visibility=local`` stays immutable and off the sink (NFR-E13-1).
        """
        fields = {name: getattr(source, name) for name in self._PROMOTABLE_FIELDS}
        # Copy mutable JSON columns so the new row never aliases the source's.
        fields["linked_entities"] = list(fields["linked_entities"])
        # Provenance records the promotion lineage. The new row SYNCS, so it must
        # not embed the local source's id (NFR-E13-1: no byte/id of the local
        # entry leaves the machine) — the precise source id lives only in the
        # local source's audit object_ref, which never syncs (audit_events are a
        # per-replica trail). So ``detail`` notes the lineage id-free.
        prov: dict[str, Any] = {
            "origin": source.source,
            "actor_id": self._actor_id,
            "captured_at": now.isoformat(),
            "detail": "promoted from a local entry",
        }
        proposed = MemoryEntry(
            created_by=self._actor_id,
            source=source.source,
            visibility="team",
            review_status="proposed",
            provenance=prov,
            created_at=now,
            updated_at=now,
            **fields,
        )
        self._session.add(proposed)
        self._session.flush()
        # Audit the new team row with its snapshot; the local source is audited
        # content-free (the lineage exists, the private content never does).
        self._audit_entry_write("memory.promote", proposed, before=None, now=now)
        self._audit_entry_write("memory.promote", source, before=None, after_none=True, now=now)
        self._emit_team_only("memory_entries", proposed, "patch", audit.snapshot(proposed))
        # The original local row never routes through the sink — promote does
        # not bypass _emit_team_only, and it is never called for the source.
        self._session.commit()
        self._session.refresh(proposed)
        return proposed

    def _promote_team_in_place(self, entry: MemoryEntry, *, now: datetime) -> MemoryEntry:
        """Transition a ``team`` ``{draft,stale}`` row to ``proposed`` in place."""
        before = audit.snapshot(entry)
        entry.review_status = "proposed"
        entry.updated_at = now
        self._session.add(entry)
        self._session.flush()
        self._audit_entry_write("memory.promote", entry, before=before, now=now)
        after = audit.snapshot(entry)
        self._emit_team_only(
            "memory_entries",
            entry,
            "patch",
            {"review_status": after["review_status"], "updated_at": after["updated_at"]},
        )
        self._session.commit()
        self._session.refresh(entry)
        return entry

    def approve(self, memory_id: str) -> MemoryEntry:
        """Approve a ``proposed`` team entry into the shared collection (HUMAN).

        ``proposed → approved``; ``domain_visibility(team, approved, …)`` then
        labels it shared. A compare-and-swap re-checking ``review_status =
        'proposed'`` guards against a concurrent decision (mirrors
        ``proposals._flip_status``)."""
        return self._decide(memory_id, "approved", "memory.approve")

    def reject(self, memory_id: str) -> MemoryEntry:
        """Decline a ``proposed`` team entry (HUMAN): ``proposed → rejected``."""
        return self._decide(memory_id, "rejected", "memory.reject")

    def _decide(self, memory_id: str, status: str, action: str) -> MemoryEntry:
        """The shared approve/reject body: a CAS status flip + audit + event.

        Mirrors ``kantaq_core.proposals._flip_status`` exactly — a conditional
        UPDATE re-checking ``review_status == 'proposed'``; a loser matches zero
        rows and raises ``MemoryConflictError`` (the double-apply guard)."""
        entry = self.get_entry(memory_id)
        before = audit.snapshot(entry)
        now = self._now()
        claimed = cast(
            "CursorResult[Any]",
            self._session.execute(
                sa_update(MemoryEntry)
                .where(
                    col(MemoryEntry.id) == entry.id,
                    col(MemoryEntry.review_status) == "proposed",
                )
                .values(review_status=status, updated_at=now)
            ),
        )
        if claimed.rowcount != 1:
            self._session.rollback()
            raise MemoryConflictError(
                f"memory entry was decided concurrently or is not proposed (action {action})"
            )
        self._session.refresh(entry)
        # Always a team row (only team rows ever reach 'proposed'); snapshots
        # are full. The approver's actor differs from the proposer's
        # (dogfood-gate #4) — it is whoever holds this service's actor_id.
        self._audit_entry_write(action, entry, before=before, now=now)
        after = audit.snapshot(entry)
        self._emit_team_only(
            "memory_entries",
            entry,
            "patch",
            {"review_status": after["review_status"], "updated_at": after["updated_at"]},
        )
        self._session.commit()
        self._session.refresh(entry)
        return entry

    def get_entry(self, memory_id: str) -> MemoryEntry:
        entry = self._session.get(MemoryEntry, memory_id)
        if entry is None:
            raise MemoryNotFoundError("memory_entries", memory_id)
        return entry

    def list_entries(
        self,
        *,
        space: str | None = None,
        type: str | None = None,  # noqa: A002 — mirrors the model field
        review_status: str | None = None,
        q: str | None = None,
        include_expired: bool = False,
    ) -> list[MemoryEntry]:
        """List entries, newest first; keyword search is DEBT-05 (substring)."""
        statement = select(MemoryEntry)
        if space is not None:
            statement = statement.where(MemoryEntry.space == space)
        if type is not None:
            statement = statement.where(MemoryEntry.type == type)
        if review_status is not None:
            statement = statement.where(MemoryEntry.review_status == review_status)
        rows = list(self._session.exec(statement).all())
        if not include_expired:
            now = self._now()
            rows = [r for r in rows if r.expires_at is None or r.expires_at > now]
        if q is not None and q.strip():
            needle = q.strip().lower()
            # Generic JSON/VARCHAR columns have no portable case-insensitive
            # contains; filter the narrowed rows in Python (DEBT-05 scale).
            rows = [r for r in rows if needle in r.title.lower() or needle in r.body.lower()]
        return sorted(rows, key=lambda r: r.id, reverse=True)

    def delete_entry(self, memory_id: str) -> None:
        """Delete an entry and its links; team rows tombstone, local rows don't."""
        entry = self.get_entry(memory_id)
        links = self.links_for_entry(memory_id)
        ts = self._now()
        for link in links:
            self._emit_team_only("memory_links", link, "tombstone", {})
            self._session.delete(link)
        before = audit.snapshot(entry) if entry.visibility == "team" else None
        self._audit_entry_write("memory.delete", entry, before=before, after_none=True, now=ts)
        self._emit_team_only("memory_entries", entry, "tombstone", {})
        self._session.delete(entry)
        self._session.commit()

    # ------------------------------------------------------------------- links

    def link(self, memory_id: str, ticket_id: str, reason: str) -> MemoryLink:
        """Manually link a ticket to a memory entry with a reason (FR-E13-2)."""
        entry = self.get_entry(memory_id)
        if self._session.get(Ticket, ticket_id) is None:
            raise MemoryNotFoundError("tickets", ticket_id)
        reason = reason.strip()
        if not reason:
            raise MemoryValidationError("a memory link needs a non-empty reason")
        if len(reason) > _REASON_MAX:
            raise MemoryValidationError(f"link reason exceeds {_REASON_MAX} characters")
        existing = self._session.exec(
            select(MemoryLink)
            .where(MemoryLink.ticket_id == ticket_id)
            .where(MemoryLink.memory_id == memory_id)
        ).first()
        if existing is not None:
            raise MemoryValidationError("this ticket and memory entry are already linked")

        ts = self._now()
        link = MemoryLink(
            ticket_id=ticket_id,
            memory_id=memory_id,
            reason=reason,
            created_by=self._actor_id,
            # The link inherits the stricter endpoint's visibility: a link to a
            # local entry is itself local and never produces an event.
            visibility=entry.visibility,
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(link)
        self._session.flush()
        if entry.visibility == "team":
            # On the ticket, so the link lands in its activity feed (MOD-07).
            audit.write(
                self._session,
                actor_id=self._actor_id,
                action="memory.link",
                source=self._source,
                object_ref=f"tickets/{ticket_id}",
                after=audit.snapshot(link),
                now=ts,
            )
        else:
            # Content-free, on the entry: the private association must not
            # enter the ticket's feed or the syncable audit collection.
            audit.write(
                self._session,
                actor_id=self._actor_id,
                action="memory.link",
                source=self._source,
                object_ref=f"memory_entries/{memory_id}",
                now=ts,
            )
        self._emit_team_only("memory_links", link, "patch", audit.snapshot(link))
        self._session.commit()
        self._session.refresh(link)
        return link

    def links_for_entry(self, memory_id: str) -> list[MemoryLink]:
        self.get_entry(memory_id)
        statement = select(MemoryLink).where(MemoryLink.memory_id == memory_id)
        return sorted(self._session.exec(statement).all(), key=lambda r: r.id)

    def linked_memory(
        self, ticket_id: str, *, include_expired: bool = False
    ) -> list[tuple[MemoryLink, MemoryEntry]]:
        """The ticket's linked entries with their link reasons (FR-E13-2/T3)."""
        if self._session.get(Ticket, ticket_id) is None:
            raise MemoryNotFoundError("tickets", ticket_id)
        links = sorted(
            self._session.exec(select(MemoryLink).where(MemoryLink.ticket_id == ticket_id)).all(),
            key=lambda r: r.id,
        )
        now = self._now()
        out: list[tuple[MemoryLink, MemoryEntry]] = []
        for link in links:
            entry = self._session.get(MemoryEntry, link.memory_id)
            if entry is None:  # pragma: no cover - delete cascades links
                continue
            if not include_expired and entry.expires_at is not None and entry.expires_at <= now:
                continue
            out.append((link, entry))
        return out

    # ----------------------------------------------------------------- helpers

    def _audit_entry_write(
        self,
        action: str,
        entry: MemoryEntry,
        *,
        before: dict[str, Any] | None,
        after_none: bool = False,
        now: datetime,
    ) -> None:
        """One attributed audit row per write (MOD-07), content-free for local.

        A local row's audit records *that* a private write happened (existence
        is auditable, §6.13) but never its content: audit_events is a syncable
        collection, so a snapshot here would be tomorrow's leak.
        """
        after: dict[str, Any] | None = None
        if not after_none and entry.visibility == "team":
            after = audit.snapshot(entry)
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action=action,
            source=self._source,
            object_ref=f"memory_entries/{entry.id}",
            before=before,
            after=after,
            now=now,
        )

    def _validated_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        if "title" in out:
            out["title"] = str(out["title"]).strip()
            if not out["title"]:
                raise MemoryValidationError("a memory entry needs a non-empty title")
            if len(out["title"]) > _TITLE_MAX:
                raise MemoryValidationError(f"memory title exceeds {_TITLE_MAX} characters")
        if "body" in out and len(str(out["body"])) > _BODY_MAX:
            raise MemoryValidationError(f"memory body exceeds {_BODY_MAX} characters")
        for fieldname, vocabulary in (
            ("type", MEMORY_TYPES),
            ("source", MEMORY_SOURCES),
            ("space", MEMORY_SPACES),
            ("confidence", CONFIDENCE_LEVELS),
            ("review_status", REVIEW_STATUSES),
        ):
            if fieldname in out and out[fieldname] not in vocabulary:
                raise MemoryValidationError(
                    f"unknown {fieldname} {out[fieldname]!r}; expected one of {vocabulary}"
                )
        if "linked_entities" in out:
            refs = out["linked_entities"]
            if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                raise MemoryValidationError("linked_entities must be a list of strings")
            cleaned: list[str] = []
            for raw in refs:
                ref = raw.strip()
                if not _ENTITY_REF.fullmatch(ref):
                    raise MemoryValidationError(
                        f"linked entity {ref[:40]!r} is not a collection/id reference"
                    )
                if ref not in cleaned:
                    cleaned.append(ref)
            out["linked_entities"] = cleaned
        if "provenance" in out:
            prov = out["provenance"]
            if not isinstance(prov, dict):
                raise MemoryValidationError("provenance must be an object")
            unknown_keys = set(prov) - _PROVENANCE_KEYS
            if unknown_keys:
                raise MemoryValidationError(
                    f"unknown provenance keys: {sorted(unknown_keys)}; "
                    f"allowed: {sorted(_PROVENANCE_KEYS)}"
                )
            if any(not isinstance(value, str) for value in prov.values()):
                raise MemoryValidationError("provenance values must be strings")
            out["provenance"] = dict(prov)
        if out.get("expires_at") is not None:
            out["expires_at"] = _naive_utc(out["expires_at"])
        return out
