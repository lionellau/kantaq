"""NFR-E13-1 (SEC): ``visibility=local`` memory never syncs — proven twice.

The guarantee is enforced at the emit seam (``kantaq_core.memory`` skips the
sink for local rows), so the proof has two layers:

1. **The event log** — the strongest claim: across create/update/link/delete a
   local entry produces *zero* ``event_log`` rows. Private content never even
   enters the thing that gets pushed.
2. **An end-to-end push** — a real ``SyncEngine`` over the MOD-30 two-replica
   simulator pushes to a FakeBackend; nothing about the local entry arrives,
   while a team entry created the same way does. A second replica that pulls
   everything still knows nothing about it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from sqlmodel import select

from kantaq_db import EventLog, MemoryEntry
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, make_replica

MEMORY_COLLECTIONS = {"memory_entries", "memory_links"}


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def alice(tmp_path: Path, backend: FakeBackend) -> Replica:
    return make_replica(tmp_path, "alice", backend)


@pytest.fixture
def bob(tmp_path: Path, backend: FakeBackend) -> Replica:
    return make_replica(tmp_path, "bob", backend)


def _ticket(replica: Replica) -> str:
    with replica.session() as session:
        service = replica.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        return service.create_ticket(project_id=project.id, title="T").id


def _memory_event_rows(replica: Replica) -> list[EventLog]:
    with replica.session() as session:
        rows = session.exec(select(EventLog)).all()
    return [row for row in rows if row.collection in MEMORY_COLLECTIONS]


def test_local_entry_never_enters_the_event_log(alice: Replica) -> None:
    """Create, update, link, delete — zero memory events for a local entry."""
    ticket_id = _ticket(alice)
    with alice.session() as session:
        memory = alice.memory_service(session)
        entry = memory.create_entry(title="my private note", visibility="local")
        memory.update_entry(entry.id, {"body": "details nobody else should see"})
        memory.link(entry.id, ticket_id, "private context")
        memory.delete_entry(entry.id)

    assert _memory_event_rows(alice) == []


def test_team_entry_events_flow_normally(alice: Replica) -> None:
    """The same flow with visibility=team produces the full event stream."""
    ticket_id = _ticket(alice)
    with alice.session() as session:
        memory = alice.memory_service(session)
        entry = memory.create_entry(title="shared note")
        memory.update_entry(entry.id, {"body": "for the team"})
        memory.link(entry.id, ticket_id, "shared context")
        memory.delete_entry(entry.id)

    ops = [(row.collection, row.op) for row in _memory_event_rows(alice)]
    assert ops == [
        ("memory_entries", "patch"),
        ("memory_entries", "patch"),
        ("memory_links", "patch"),
        ("memory_links", "tombstone"),
        ("memory_entries", "tombstone"),
    ]


def test_push_carries_team_but_never_local(alice: Replica, backend: FakeBackend) -> None:
    """The sync-push half of NFR-E13-1, asserted against the backend bytes."""
    ticket_id = _ticket(alice)
    with alice.session() as session:
        memory = alice.memory_service(session)
        local = memory.create_entry(title="local-secret-marker", visibility="local")
        memory.update_entry(local.id, {"body": "local-body-marker"})
        memory.link(local.id, ticket_id, "local-reason-marker")
        team = memory.create_entry(title="team note")
        local_id = local.id

    alice.sync.push()

    pushed = backend.pull(collection=None, since=0)
    payload_dump = json.dumps([asdict(entry.event) for entry in pushed], default=str)
    assert "local-secret-marker" not in payload_dump
    assert "local-body-marker" not in payload_dump
    assert "local-reason-marker" not in payload_dump
    assert local_id not in payload_dump
    # The team entry made the trip — the channel itself works.
    assert any(
        entry.event.collection == "memory_entries" and entry.event.entity_id == team.id
        for entry in pushed
    )


def test_other_replica_never_learns_the_local_entry(
    alice: Replica, bob: Replica, backend: FakeBackend
) -> None:
    _ticket(alice)
    with alice.session() as session:
        memory = alice.memory_service(session)
        local = memory.create_entry(title="private", visibility="local")
        team = memory.create_entry(title="public-to-team")
        local_id, team_id = local.id, team.id

    alice.sync.push()
    bob.sync.pull()

    with bob.session() as session:
        assert session.get(MemoryEntry, local_id) is None
        replicated = session.get(MemoryEntry, team_id)
        assert replicated is not None and replicated.title == "public-to-team"
