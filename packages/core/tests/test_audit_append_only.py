"""MOD-07 append-only enforcement (E07-T2, NFR-E07-1): no update/delete path.

The guards are probed at every depth the second (security) review attacked:
unit-of-work, bulk ORM statements, legacy ``bulk_update_mappings``,
table-targeted statements, bare-connection statements, and sessions created by
an independent ``sessionmaker`` on a different engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, SQLModel, delete, select, update

from kantaq_core import audit
from kantaq_db import AuditEvent, Ticket


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


@pytest.fixture
def audit_row(session: Session) -> AuditEvent:
    row = audit.write(session, actor_id="mbr_alice", action="ticket.update", source="app")
    session.commit()
    return row


def test_modifying_an_audit_row_fails_on_flush(session: Session, audit_row: AuditEvent) -> None:
    audit_row.action = "ticket.delete"
    with pytest.raises(audit.AppendOnlyAuditError):
        session.commit()
    session.rollback()
    assert session.exec(select(AuditEvent)).one().action == "ticket.update"


def test_deleting_an_audit_row_fails_on_flush(session: Session, audit_row: AuditEvent) -> None:
    session.delete(audit_row)
    with pytest.raises(audit.AppendOnlyAuditError):
        session.commit()
    session.rollback()
    assert session.exec(select(AuditEvent)).one().id == audit_row.id


def test_bulk_update_of_audit_rows_is_refused(session: Session, audit_row: AuditEvent) -> None:
    with pytest.raises(audit.AppendOnlyAuditError):
        session.execute(update(AuditEvent).values(action="tampered"))
    session.rollback()
    assert session.exec(select(AuditEvent)).one().action == "ticket.update"


def test_bulk_delete_of_audit_rows_is_refused(session: Session, audit_row: AuditEvent) -> None:
    with pytest.raises(audit.AppendOnlyAuditError):
        session.execute(delete(AuditEvent))
    session.rollback()
    assert session.exec(select(AuditEvent)).one().id == audit_row.id


def test_bulk_update_mappings_is_refused(session: Session, audit_row: AuditEvent) -> None:
    """The legacy bulk API skips mapper events AND do_orm_execute (review B1)."""
    with pytest.raises(audit.AppendOnlyAuditError):
        session.bulk_update_mappings(AuditEvent, [{"id": audit_row.id, "action": "tampered"}])
    session.rollback()
    assert session.exec(select(AuditEvent)).one().action == "ticket.update"


def test_table_targeted_statement_is_refused(session: Session, audit_row: AuditEvent) -> None:
    """update(Table) has no bind_mapper, dodging the ORM hook (review S1)."""
    with pytest.raises(audit.AppendOnlyAuditError):
        session.execute(update(AuditEvent.__table__).values(action="tampered"))
    session.rollback()
    assert session.exec(select(AuditEvent)).one().action == "ticket.update"


def test_bare_connection_statement_is_refused(session: Session, audit_row: AuditEvent) -> None:
    """Compiled DML on the connection under the session is still caught."""
    with pytest.raises(audit.AppendOnlyAuditError):
        session.connection().execute(delete(AuditEvent.__table__))
    session.rollback()
    assert session.exec(select(AuditEvent)).one().id == audit_row.id


def test_guards_cover_foreign_sessions_and_engines(tmp_path: Path) -> None:
    """Class-level guards apply to sessions/engines this module never created."""
    engine = create_engine(f"sqlite:///{tmp_path / 'other.sqlite'}")
    SQLModel.metadata.create_all(engine)
    factory = sessionmaker(class_=Session, bind=engine)
    with factory() as session:
        row = audit.write(session, actor_id="mbr_bob", action="ticket.update", source="cli")
        session.commit()
        row.action = "tampered"
        with pytest.raises(audit.AppendOnlyAuditError):
            session.commit()
        session.rollback()
        assert session.exec(select(AuditEvent)).one().action == "ticket.update"
    engine.dispose()


def test_other_collections_still_update_and_delete(session: Session) -> None:
    """The guard is scoped to audit_events; normal domain writes are untouched."""
    ticket = Ticket(project_id="prj_1", title="A ticket")
    session.add(ticket)
    session.commit()

    ticket.title = "Renamed"
    session.commit()
    assert session.exec(select(Ticket)).one().title == "Renamed"

    session.delete(ticket)
    session.commit()
    assert session.exec(select(Ticket)).first() is None
