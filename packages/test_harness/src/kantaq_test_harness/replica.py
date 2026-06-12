"""The two-replica simulator (MOD-30, named by MOD-04's harness profile).

A ``Replica`` is one member's world: their own SQLite, their tracker service
writing through an ``EventLogSink``, and a ``SyncEngine`` pointed at a shared
``FakeBackend``. Two of these plus one backend is the two-runtime topology the
sync tests (MOD-04) and later the runtime/UI integration tests stand on.

Both replicas are seeded with the same workspace row — the team-manifest
baseline members share out of band; everything after that moves only through
events. Unlike the leaf fakes (clock, random, backend), this module composes
the real packages on purpose: a simulator that faked the domain would prove
nothing about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from kantaq_core.memory import MemoryService
from kantaq_core.tracker import TrackerService
from kantaq_db import Workspace
from kantaq_sync_engine import EventLogSink, SyncEngine
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.clock import FakeClock

WORKSPACE_ID = "ws_shared0000000000000000"


@dataclass
class Replica:
    """One member's runtime-in-miniature for sync tests."""

    name: str
    db: Engine
    actor_id: str
    sync: SyncEngine
    clock: FakeClock

    def service(self, session: Session) -> TrackerService:
        """A tracker service writing through this replica's event log."""
        return TrackerService(
            session,
            actor_id=self.actor_id,
            source="app",
            sink=EventLogSink(session, self.actor_id),
            now=self.clock.now,
        )

    def memory_service(self, session: Session) -> MemoryService:
        """A memory service writing through this replica's event log (E13)."""
        return MemoryService(
            session,
            actor_id=self.actor_id,
            source="app",
            sink=EventLogSink(session, self.actor_id),
            now=self.clock.now,
        )

    def session(self) -> Session:
        return Session(self.db)


def _seed(db: Engine) -> None:
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        session.add(Workspace(id=WORKSPACE_ID, name="Shared Workspace"))
        session.commit()


def _build(name: str, db: Engine, backend: FakeBackend, clock: FakeClock | None) -> Replica:
    _seed(db)
    actor_id = f"mbr_{name.ljust(22, '0')}"
    return Replica(
        name=name,
        db=db,
        actor_id=actor_id,
        sync=SyncEngine(db, backend, actor_id=actor_id),
        clock=clock or FakeClock(),
    )


def make_replica(
    tmp_path: Path, name: str, backend: FakeBackend, *, clock: FakeClock | None = None
) -> Replica:
    """A file-backed replica under ``tmp_path`` (WAL behaves like production)."""
    db = create_engine(f"sqlite:///{tmp_path / f'replica-{name}.sqlite'}")
    return _build(name, db, backend, clock)


def memory_replica(name: str, backend: FakeBackend, *, clock: FakeClock | None = None) -> Replica:
    """An in-memory replica — what property tests build per drawn example."""
    return _build(name, create_engine("sqlite://"), backend, clock)
