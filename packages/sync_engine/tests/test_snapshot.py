"""E04-T3 — NDJSON snapshot compose: deterministic, round-trips, equals state."""

from __future__ import annotations

import json

from sqlmodel import select

from kantaq_core import audit
from kantaq_db import Ticket
from kantaq_sync_engine import compose_snapshot, parse_snapshot
from kantaq_test_harness.replica import WORKSPACE_ID, Replica


def _seed(replica: Replica) -> str:
    with replica.session() as session:
        service = replica.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        a = service.create_ticket(project_id=project.id, title="A", labels=["bug"])
        service.create_ticket(project_id=project.id, title="B", priority="high")
        service.update_ticket(a.id, {"status": "doing"})
        return project.id


def test_snapshot_is_valid_sorted_ndjson(alice: Replica) -> None:
    _seed(alice)
    with alice.session() as session:
        ndjson = compose_snapshot(session, "tickets")

    lines = ndjson.strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert all(set(r) == {"collection", "entity_id", "state"} for r in records)
    assert [r["entity_id"] for r in records] == sorted(r["entity_id"] for r in records)


def test_snapshot_is_deterministic(alice: Replica) -> None:
    _seed(alice)
    with alice.session() as session:
        first = compose_snapshot(session, "tickets")
    with alice.session() as session:
        second = compose_snapshot(session, "tickets")
    assert first == second


def test_snapshot_round_trips_through_parse(alice: Replica) -> None:
    _seed(alice)
    with alice.session() as session:
        state = parse_snapshot(compose_snapshot(session, "tickets"))
        rows = session.exec(select(Ticket)).all()
        assert set(state) == {row.id for row in rows}


def test_snapshot_equals_the_table_state(alice: Replica) -> None:
    """The MOD-04 acceptance: snapshot + log reconstruct identical state.

    The table rows are the fold of the log (MOD-03's property); the snapshot
    is composed from the log independently — they must agree field by field.
    """
    _seed(alice)
    with alice.session() as session:
        state = parse_snapshot(compose_snapshot(session, "tickets"))
        for row in session.exec(select(Ticket)).all():
            assert state[row.id] == audit.snapshot(row), row.title


def test_empty_collection_composes_an_empty_snapshot(alice: Replica) -> None:
    with alice.session() as session:
        assert compose_snapshot(session, "agent_proposals") == ""
        assert parse_snapshot("") == {}
