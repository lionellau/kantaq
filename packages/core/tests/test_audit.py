"""MOD-07 audit: attributed writes, snapshots, validation (E07-T1, FR-E07-1..3)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core import audit
from kantaq_db import AuditEvent, Ticket
from kantaq_test_harness import AuditCapture, FakeClock


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


def test_write_persists_an_attributed_row(session: Session, fake_clock: FakeClock) -> None:
    row = audit.write(
        session,
        actor_id="mbr_alice",
        action="ticket.update",
        source="app",
        object_ref="tkt_1",
        before={"status": "todo"},
        after={"status": "in_progress"},
        now=fake_clock.now(),
    )
    session.commit()

    stored = session.exec(select(AuditEvent)).one()
    assert stored.id == row.id
    assert stored.actor_id == "mbr_alice"
    assert stored.action == "ticket.update"
    assert stored.object_ref == "tkt_1"
    assert stored.before == {"status": "todo"}
    assert stored.after == {"status": "in_progress"}
    assert stored.source == "app"
    # SQLite stores datetimes naive; the instant must match the injected clock.
    assert stored.created_at == fake_clock.now().replace(tzinfo=None)


def test_every_human_write_gets_its_own_row_no_gaps(
    session: Session, fake_clock: FakeClock
) -> None:
    capture = AuditCapture(session)
    for i in range(7):
        audit.write(
            session,
            actor_id="mbr_alice",
            action="ticket.update",
            source="app",
            object_ref=f"tkt_{i}",
            now=fake_clock.now(),
        )
        fake_clock.advance(1)

    rows = capture.by_actor("mbr_alice")
    assert len(rows) == 7
    assert [row["object_ref"] for row in rows] == [f"tkt_{i}" for i in range(7)]


def test_write_requires_an_actor(session: Session) -> None:
    with pytest.raises(audit.AuditWriteError, match="actor_id"):
        audit.write(session, actor_id="  ", action="ticket.update", source="app")


def test_write_requires_an_action(session: Session) -> None:
    with pytest.raises(audit.AuditWriteError, match="action"):
        audit.write(session, actor_id="mbr_alice", action="", source="app")


def test_write_rejects_an_oversized_action(session: Session) -> None:
    with pytest.raises(audit.AuditWriteError, match="64"):
        audit.write(session, actor_id="mbr_alice", action="x" * 65, source="app")


def test_write_rejects_an_unknown_source(session: Session) -> None:
    with pytest.raises(audit.AuditWriteError, match="source"):
        audit.write(session, actor_id="mbr_alice", action="ticket.update", source="webhook")


def test_snapshot_is_json_safe(session: Session) -> None:
    ticket = Ticket(project_id="prj_1", title="Fix login")
    snap = audit.snapshot(ticket)

    assert snap["title"] == "Fix login"
    assert isinstance(snap["created_at"], str)  # datetime serialized, JSON-storable
    audit.write(session, actor_id="mbr_alice", action="ticket.create", source="app", after=snap)
    session.commit()

    stored = session.exec(select(AuditEvent)).one()
    assert stored.after is not None
    assert stored.after["title"] == "Fix login"


def test_write_requires_an_explicit_source(session: Session) -> None:
    """No default source: a forgotten kwarg must not silently attribute to "app"."""
    with pytest.raises(TypeError, match="source"):
        audit.write(session, actor_id="mbr_alice", action="ticket.update")  # type: ignore[call-arg]
