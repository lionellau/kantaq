"""Boot-time member self-announce (DEBT-45, docs/design/member-events.md §2a).

``members`` was a syncable collection everywhere except the write path: rows
were created directly (backend ``seed``, replica ``adopt_owner``) and never
entered the event log, so a peer pulling this runtime's ``devices`` event had
no ``members`` row for its ``member_id`` FK. The seam that closes it lives
next to the device emit it exists for: at boot, before the device event, the
runtime announces the member it *is*.

Self only, by construction: the sync server binds ``actor == the token's
member`` (DEBT-42), and an announce's ``entity_id`` equals its ``actor_id`` —
the verify layer's self-announce carve-out is pinned to exactly that shape.
"""

from __future__ import annotations

from sqlmodel import Session

from kantaq_core import audit
from kantaq_core.tracker.events import DomainEvent, EventSink
from kantaq_db.models import Member
from kantaq_sync_engine.log import entity_rows


def ensure_member_event(session: Session, member: Member, sink: EventSink) -> bool:
    """Announce this runtime's own member row into the event log, once.

    Idempotent across boots by a log check (mirrors ``ensure_device``'s
    row check beside it): the first boot emits, every later boot finds the
    event and returns ``False``. An existing pre-fix runtime backfills on its
    next boot — the §6 self-heal, no migration. The payload is the full row
    snapshot (``op="patch"``: the fold materialises absent entities), the
    exact posture of the device emit that follows it, and it must be emitted
    **before** that device event so fresh streams fold member-then-device.
    """
    if entity_rows(session, "members", member.id):
        return False
    sink.emit(
        DomainEvent(
            collection="members",
            entity_id=member.id,
            op="patch",
            payload=audit.snapshot(member),
        )
    )
    return True
