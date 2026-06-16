"""E23-T3 (MOD-23 / FR-E23-4): the Linear importer over the JobWinAI shape.

Uses the synthetic JobWinAI-shaped export (the real one is private — DEBT-17).
Pins: the status → stage/status mapping (MOD-20), Parent → parent_id, comments
+ light threading, the named edge cases, and idempotency on the deterministic id.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, col, func, select

from kantaq_db.models import Comment, Project, Ticket, Workspace
from kantaq_runtime.linear_import import import_linear, linear_entity_id
from kantaq_test_harness.linear_fixture import build_linear_export

WS = "ws" + "0" * 24
PRJ = "prj" + "0" * 23
ACTOR = "mbr" + "0" * 23


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        session.add(Workspace(id=WS, name="Acme"))
        session.add(Project(id=PRJ, workspace_id=WS, name="Imported"))
        session.commit()
        yield session


def _import(session: Session, payload: dict) -> object:
    return import_linear(payload, session=session, workspace_id=WS, project_id=PRJ, actor_id=ACTOR)


def test_jobwinai_shape_imports_clean(session: Session) -> None:
    payload = build_linear_export()
    result = _import(session, payload)

    assert result.tickets == 269
    assert result.epics == 26
    assert result.parent_links == 185
    assert result.comments == 407
    # Every ticket and comment landed in the local replica.
    assert session.exec(select(func.count()).select_from(Ticket)).one() == 269


def test_status_maps_to_stage_and_state(session: Session) -> None:
    payload = build_linear_export()
    _import(session, payload)
    by_status = {t["status"]: t["id"] for t in payload["tickets"]}

    done = session.get(Ticket, linear_entity_id(WS, "ticket", by_status["Done"]))
    assert done is not None and done.status == "done" and done.lifecycle_stage == "learn"

    canceled = session.get(Ticket, linear_entity_id(WS, "ticket", by_status["Canceled"]))
    assert (
        canceled is not None and canceled.status == "done" and canceled.lifecycle_stage == "learn"
    )

    backlog = session.get(Ticket, linear_entity_id(WS, "ticket", by_status["Backlog"]))
    assert backlog is not None and backlog.status == "todo" and backlog.lifecycle_stage == "intake"


def test_parent_maps_to_native_parent_id(session: Session) -> None:
    payload = build_linear_export()
    _import(session, payload)
    # A child with a Linear parent gets parent_id = the parent's derived id.
    child_raw = next(t for t in payload["tickets"] if t["parent"])
    child = session.get(Ticket, linear_entity_id(WS, "ticket", child_raw["id"]))
    assert child is not None
    assert child.parent_id == linear_entity_id(WS, "ticket", str(child_raw["parent"]))
    # An [Epic] parent therefore has children.
    parents = {t.parent_id for t in session.exec(select(Ticket)).all() if t.parent_id is not None}
    assert len(parents) > 0


def test_edge_cases_multi_label_and_threading(session: Session) -> None:
    payload = build_linear_export()
    _import(session, payload)
    # Multi-label ticket (every 3rd in the fixture carries 2 labels).
    multi_raw = next(t for t in payload["tickets"] if len(t["labels"]) >= 2)
    multi = session.get(Ticket, linear_entity_id(WS, "ticket", multi_raw["id"]))
    assert multi is not None and len(multi.labels) >= 2
    # Light threading: a reply comment folds the context into its body.
    threaded = session.exec(select(Comment).where(col(Comment.body).like("↳ in reply to%"))).first()
    assert threaded is not None


def test_reimport_is_idempotent(session: Session) -> None:
    payload = build_linear_export()
    _import(session, payload)
    again = _import(session, payload)

    assert again.tickets == 0
    assert again.comments == 0
    assert again.parent_links == 0
    assert again.skipped_tickets == 269
    assert again.skipped_comments == 407
    # No duplication: still exactly the original counts.
    assert session.exec(select(func.count()).select_from(Ticket)).one() == 269
