"""E04 property test: two replicas converge under any interleaving (MOD-04).

The Sync-profile property (test-harness standard §4): for any sequence of
tracker mutations on either replica, interleaved with partial sync rounds, a
final full sync leaves both replicas byte-identical (their NDJSON snapshots)
and in agreement with the backend's own fold on every patched scalar field.

Hypothesis draws the interleaving; each example builds a fresh hermetic world
(two in-memory replicas, one FakeBackend), so examples cannot bleed into each
other and the outcome is a pure function of the drawn sequence.
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlmodel import Session, SQLModel, create_engine, select

from kantaq_core.tracker import TrackerService
from kantaq_db import Ticket, Workspace
from kantaq_sync_engine import SyncEngine, compose_snapshot
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.replica import WORKSPACE_ID, Replica

# One drawn step: which replica acts and what it does. "sync" is a partial
# round (that one replica pushes and pulls), so histories genuinely interleave.
_step = st.fixed_dictionaries(
    {
        "replica": st.integers(min_value=0, max_value=1),
        "action": st.sampled_from(["create", "status", "priority", "comment", "sync"]),
        "status": st.sampled_from(["todo", "doing", "done"]),
        "priority": st.sampled_from(["low", "medium", "high", "urgent"]),
    }
)


def _memory_replica(name: str, backend: FakeBackend) -> Replica:
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        session.add(Workspace(id=WORKSPACE_ID, name="Shared Workspace"))
        session.commit()
    actor_id = f"mbr_{name.ljust(22, '0')}"
    return Replica(
        name=name,
        db=db,
        actor_id=actor_id,
        sync=SyncEngine(db, backend, actor_id=actor_id),
        clock=FakeClock(),
    )


def _tickets(replica: Replica) -> list[str]:
    with replica.session() as session:
        return sorted(row.id for row in session.exec(select(Ticket)).all())


def _apply_step(replica: Replica, step: dict[str, Any], project_id: str, counter: int) -> None:
    if step["action"] == "sync":
        replica.sync.push()
        replica.sync.pull()
        return
    tickets = _tickets(replica)
    with replica.session() as session:
        service: TrackerService = replica.service(session)
        if step["action"] == "create" or not tickets:
            service.create_ticket(project_id=project_id, title=f"T{counter} {replica.name}")
            return
        target = tickets[counter % len(tickets)]
        if step["action"] == "status":
            service.update_ticket(target, {"status": step["status"]})
        elif step["action"] == "priority":
            service.update_ticket(target, {"priority": step["priority"]})
        else:
            service.add_comment(target, f"c{counter} from {replica.name}")


def _full_sync(replicas: list[Replica]) -> None:
    # Online v0.0.5: two push+pull rounds — the second delivers events that
    # were committed between the first round's pushes.
    for _ in range(2):
        for replica in replicas:
            replica.sync.push()
        for replica in replicas:
            replica.sync.pull()


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=st.lists(_step, min_size=1, max_size=12))
def test_two_replicas_converge(steps: list[dict[str, Any]]) -> None:
    backend = FakeBackend()
    alice = _memory_replica("alice", backend)
    bob = _memory_replica("bob", backend)

    # Alice creates the shared project; a first full sync hands it to Bob.
    with alice.session() as session:
        project_id = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P").id
    _full_sync([alice, bob])

    replicas = [alice, bob]
    for counter, step in enumerate(steps):
        _apply_step(replicas[step["replica"]], step, project_id, counter)

    _full_sync(replicas)

    # Byte-identical replicas, collection by collection.
    for collection in ("projects", "tickets", "comments"):
        with alice.session() as a_session, bob.session() as b_session:
            assert compose_snapshot(a_session, collection) == compose_snapshot(
                b_session, collection
            ), collection

    # And both agree with the backend's own fold on the patched scalars.
    backend_state = backend.snapshot("tickets")
    with alice.session() as session:
        for row in session.exec(select(Ticket)).all():
            folded = backend_state[row.id]
            assert folded["status"] == row.status
            assert folded["priority"] == row.priority
            assert folded["title"] == row.title


def test_convergence_is_idempotent_after_quiescence(tmp_path: Any) -> None:
    """Extra syncs after convergence change nothing (a fixed point)."""
    backend = FakeBackend()
    alice = _memory_replica("alice2", backend)
    bob = _memory_replica("bob2", backend)
    with alice.session() as session:
        project_id = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P").id
        alice.service(session).create_ticket(project_id=project_id, title="T")
    _full_sync([alice, bob])

    with bob.session() as session:
        before = compose_snapshot(session, "tickets")
    _full_sync([alice, bob])
    with bob.session() as session:
        assert compose_snapshot(session, "tickets") == before
