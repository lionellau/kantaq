"""The two-replica simulator wires a real tracker, log, and backend together."""

from __future__ import annotations

from pathlib import Path

from kantaq_db import Ticket
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, make_replica, memory_replica


def test_replicas_share_state_only_through_the_backend(tmp_path: Path) -> None:
    backend = FakeBackend()
    alice = make_replica(tmp_path, "alice", backend)
    bob = memory_replica("bob", backend)

    with alice.session() as session:
        service = alice.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = service.create_ticket(project_id=project.id, title="T")

    with bob.session() as session:
        assert session.get(Ticket, ticket.id) is None  # nothing leaked sideways

    alice.sync.push()
    bob.sync.pull()

    with bob.session() as session:
        replicated = session.get(Ticket, ticket.id)
        assert replicated is not None and replicated.title == "T"


def test_replicas_have_distinct_actors() -> None:
    backend = FakeBackend()
    assert memory_replica("a", backend).actor_id != memory_replica("b", backend).actor_id
