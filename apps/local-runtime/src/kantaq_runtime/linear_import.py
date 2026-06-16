"""Linear → kantaq importer (E23-T3, MOD-23 / FR-E23-4).

Maps a Linear project export to kantaq protocol collections — not a vendor
schema (architecture §8):

- a Linear **status** maps onto kantaq's two axes: ``ticket.status`` (done-ness)
  and ``ticket.lifecycle_stage`` via the locked ``LINEAR_STATUS_TO_STAGE``
  (MOD-20); both terminal statuses (Done/Canceled) land at ``learn``;
- a Linear **Parent** maps to ``Ticket.parent_id`` (the native sub-issue FK) —
  *not* a typed ``ticket_relationship`` (``parent`` is not one of the five
  relation types; the MOD-23/MOD-20 wording "a ticket_relationship" is
  reconciled to the as-built ``parent_id`` here, recorded as D-19);
- Linear **comments** map to the comment feed; light threading (``reply_to``) is
  folded into the body since kantaq has no native thread column yet.

**Idempotent on its key** (D-19): every entity's kantaq id is a deterministic,
domain-separated hash of the workspace + the Linear id, so a re-import derives
the same ids and skips what already exists — a re-run never duplicates. Unmapped
Linear-only fields (estimate, external URL, archived, the per-state timestamps)
have no kantaq home and are dropped; the importer reports what it wrote.

Writes rows + events directly (the bundle-import pattern, preserving the
deterministic ids), mapping every value into kantaq's vocabulary up front so the
written state is always valid. The caller owns the transaction's commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.lifecycle import LINEAR_STATUS_TO_STAGE
from kantaq_core.tracker.events import DomainEvent
from kantaq_db.ids import encode_base32
from kantaq_db.models import Comment, Ticket
from kantaq_sync_engine import EventLogSink, EventSigner

_ID_DOMAIN = b"kantaq:linear-import:v1\x00"

# Linear status → kantaq ticket.status (done-ness). The lifecycle stage carries
# the rest of the position via LINEAR_STATUS_TO_STAGE.
_STATUS_TO_STATE = {
    "Backlog": "todo",
    "In Progress": "doing",
    "In Review": "doing",
    "Done": "done",
    "Canceled": "done",
}
_PRIORITY = {
    "urgent": "urgent",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "no priority": "low",
}


@dataclass
class LinearImportResult:
    tickets: int = 0
    comments: int = 0
    parent_links: int = 0
    epics: int = 0
    skipped_tickets: int = 0
    skipped_comments: int = 0


class LinearImportError(ValueError):
    """The Linear payload was missing required structure."""


def linear_entity_id(workspace_id: str, kind: str, external_id: str) -> str:
    """A deterministic 26-char kantaq id for a Linear entity (the idempotency key).

    Domain-separated SHA-256 over (workspace, kind, Linear id), Crockford-encoded
    to fit ``CollectionBase.id`` — so a re-import re-derives the same id and the
    existence check skips it (D-19). Mirrors ``conflict_record_id``.
    """
    canonical = f"{workspace_id}\x1f{kind}\x1f{external_id}".encode()
    value = int.from_bytes(sha256(_ID_DOMAIN + canonical).digest()[:17], "big") & ((1 << 130) - 1)
    return encode_base32(value, 26)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _labels(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(";") if part.strip()]
    return []


def _parse_dt(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _is_epic(ticket: dict[str, Any]) -> bool:
    return str(ticket.get("title", "")).strip().startswith("[Epic]")


def _comment_body(comment: dict[str, Any]) -> str:
    body = str(comment.get("body", ""))
    reply_to = comment.get("reply_to")
    if reply_to:  # light threading folded into the body (no native thread column)
        return f"↳ in reply to {reply_to}:\n\n{body}"
    return body


def import_linear(
    payload: dict[str, Any],
    *,
    session: Session,
    workspace_id: str,
    project_id: str,
    actor_id: str,
    source: str = "cli",
    signer: EventSigner | None = None,
    now: datetime | None = None,
) -> LinearImportResult:
    """Import a parsed Linear export into ``project_id``; idempotent on re-run."""
    tickets = payload.get("tickets")
    if not isinstance(tickets, list):
        raise LinearImportError("Linear payload must carry a 'tickets' list")
    comments = payload.get("comments") or []
    ts = now or _now()
    sink = EventLogSink(session, actor_id, signer=signer)
    result = LinearImportResult()

    id_for: dict[str, str] = {}  # Linear id → kantaq id (all tickets)
    created: dict[str, str] = {}  # Linear id → kantaq id (newly created this run)

    # Pass 1: tickets without parent_id (the FK needs parents to exist first).
    for raw in tickets:
        linear_id = str(raw["id"])
        tid = linear_entity_id(workspace_id, "ticket", linear_id)
        id_for[linear_id] = tid
        if session.get(Ticket, tid) is not None:
            result.skipped_tickets += 1
            continue
        status_raw = str(raw.get("status") or "Backlog")
        ticket = Ticket(
            id=tid,
            project_id=project_id,
            title=str(raw.get("title") or "(untitled)"),
            description=str(raw.get("description") or ""),
            status=_STATUS_TO_STATE.get(status_raw, "todo"),
            priority=_PRIORITY.get(str(raw.get("priority") or "").lower(), "medium"),
            labels=_labels(raw.get("labels")),
            assignee=str(raw["assignee"]) if raw.get("assignee") else None,
            due_date=_parse_dt(raw.get("due_date")),
            lifecycle_stage=LINEAR_STATUS_TO_STAGE.get(status_raw, "intake"),
            created_by=actor_id,
            created_at=ts,
            updated_at=ts,
        )
        session.add(ticket)
        session.flush()
        audit.write(
            session,
            actor_id=actor_id,
            action="ticket.create",
            source=source,
            object_ref=f"tickets/{tid}",
            after=audit.snapshot(ticket),
            now=ts,
        )
        sink.emit(DomainEvent("tickets", tid, "patch", audit.snapshot(ticket)))
        created[linear_id] = tid
        result.tickets += 1
        if _is_epic(raw):
            result.epics += 1

    # Pass 2: parent links for the tickets we just created (skip if the parent
    # wasn't in this import). Only new tickets get patched → re-import is a no-op.
    for raw in tickets:
        linear_id = str(raw["id"])
        parent = raw.get("parent")
        if not parent or linear_id not in created:
            continue
        parent_tid = id_for.get(str(parent))
        if parent_tid is None:
            continue
        tid = created[linear_id]
        child = session.get(Ticket, tid)
        if child is None:
            continue
        child.parent_id = parent_tid
        child.updated_at = ts
        session.add(child)
        session.flush()
        sink.emit(DomainEvent("tickets", tid, "patch", {"parent_id": parent_tid}))
        result.parent_links += 1

    # Comments → the comment feed; idempotent on a deterministic comment id.
    for raw in comments:
        linear_ticket = str(raw.get("ticket_id") or "")
        comment_tid = id_for.get(linear_ticket)
        if comment_tid is None or session.get(Ticket, comment_tid) is None:
            continue  # orphan comment (its ticket isn't in this import)
        key = str(raw.get("id") or f"{linear_ticket}:{raw.get('created') or result.comments}")
        cid = linear_entity_id(workspace_id, "comment", key)
        if session.get(Comment, cid) is not None:
            result.skipped_comments += 1
            continue
        comment = Comment(
            id=cid,
            ticket_id=comment_tid,
            author_actor_id=actor_id,
            body=_comment_body(raw),
            created_at=ts,
            updated_at=ts,
        )
        session.add(comment)
        session.flush()
        audit.write(
            session,
            actor_id=actor_id,
            action="comment.create",
            source=source,
            object_ref=f"comments/{cid}",
            after=audit.snapshot(comment),
            now=ts,
        )
        sink.emit(DomainEvent("comments", cid, "append", audit.snapshot(comment)))
        result.comments += 1

    session.commit()
    return result
