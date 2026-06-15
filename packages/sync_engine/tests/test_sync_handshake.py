"""E05-T2.7: the sync-version/schema-version handshake (MOD-26 §B7 / DEBT-09).

Before any drain or ingest the engine exchanges versions with the backend and
refuses a peer more than one version away — non-destructively, so the durable
outbox is never touched and the refusal is audited. A peer within ±1 is
tolerated (staggered team rollout), and same-version is the no-op default.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from kantaq_db import AuditEvent
from kantaq_sync_engine import SYNC_VERSION, SyncVersionUnsupported, pending_rows
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, memory_replica


def _queue_one_write(alice) -> None:
    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        alice.service(session).create_ticket(project_id=project.id, title="T")
        session.commit()


def test_same_version_peer_syncs_normally() -> None:
    backend = FakeBackend()  # advertises SYNC_VERSION by default
    alice = memory_replica("alice", backend)
    _queue_one_write(alice)

    flush = alice.sync.flush_outbox()

    assert flush.drained
    assert alice.sync.negotiate_session().sync_version == SYNC_VERSION


def test_peer_one_version_ahead_is_tolerated() -> None:
    backend = FakeBackend()
    backend.sync_version = SYNC_VERSION + 1  # within the ±1 rollout window
    alice = memory_replica("alice", backend)
    _queue_one_write(alice)

    flush = alice.sync.flush_outbox()

    assert flush.drained  # the staggered-upgrade peer still converges


def test_out_of_range_peer_is_refused_without_touching_the_outbox() -> None:
    backend = FakeBackend()
    backend.sync_version = SYNC_VERSION + 2  # beyond the tolerated skew
    alice = memory_replica("alice", backend)
    _queue_one_write(alice)

    with pytest.raises(SyncVersionUnsupported):
        alice.sync.flush_outbox()

    # Non-destructive: the write is still pending (never drained) and the refusal
    # is on the audit trail. Nothing reached the backend.
    with alice.session() as session:
        assert len(pending_rows(session)) == 2  # project + ticket, untouched
        rejections = session.exec(
            select(AuditEvent).where(AuditEvent.action == "sync.version_rejected")
        ).all()
        assert len(rejections) == 1
    assert len(backend) == 0
