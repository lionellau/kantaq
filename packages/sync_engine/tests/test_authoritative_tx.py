"""E05-T1 (MOD-26 §B1): authoritative_tx writes are never queued optimistically.

Grants and tokens commit synchronously through the atomic RPC — a committed
revision before the local row reflects them — so an offline self-issued grant
can never sit in the durable outbox and be trusted by the local gateway before
it syncs (the DEBT-15(a)/(b) self-escalation path). The EventLogSink refuses
them at the seam; the full synchronous-RPC-commit-at-issuance + gateway-trust
closure is the coordinated DEBT-15 follow-on.

B6 (base_rev precondition) is exercised by the signed-events suite: a signed
domain event carries base_rev (the entity's committed head, or None = genesis
for a first write), and the conflict-mode path requires the signed path
(SigningRequiredError when require_signed and no signer). Here we pin the
genesis case directly.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from kantaq_core.tracker.events import DomainEvent
from kantaq_db import EventLog
from kantaq_sync_engine import AppendOnlyWriteError, AuthoritativeWriteError, EventLogSink
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import WORKSPACE_ID, memory_replica


def test_authoritative_tx_collections_cannot_enter_the_optimistic_outbox() -> None:
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    with alice.session() as session:
        sink = EventLogSink(session, alice.actor_id)
        for collection, entity in (("capability_grants", "grt_x"), ("tokens", "tok_x")):
            with pytest.raises(AuthoritativeWriteError):
                sink.emit(
                    DomainEvent(
                        collection=collection,
                        entity_id=entity,
                        op="patch",
                        payload={"resource": WORKSPACE_ID},
                    )
                )
        # Nothing was queued — the outbox is untouched.
        assert session.exec(select(EventLog)).all() == []


def test_append_only_collections_reject_patch_and_tombstone() -> None:
    """append_only (comments) is created once, never patched (MOD-26 §B3) — the
    sink allows only an 'append' op, enforced not assumed."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    with alice.session() as session:
        sink = EventLogSink(session, alice.actor_id)
        for op in ("patch", "tombstone"):
            with pytest.raises(AppendOnlyWriteError):
                sink.emit(
                    DomainEvent(
                        collection="comments", entity_id="cmt_x", op=op, payload={"body": "x"}
                    )
                )
        # An append is allowed (the legitimate create path).
        sink.emit(
            DomainEvent(
                collection="comments", entity_id="cmt_x", op="append", payload={"body": "hi"}
            )
        )
        assert [r.op for r in session.exec(select(EventLog)).all()] == ["append"]


def test_unsigned_first_write_carries_a_genesis_base_rev() -> None:
    """B6 floor: with no signer, base_rev is None — the merge point treats a
    None base_rev as genesis (B = 0), so an intervening committed field write is
    a conflict, never a silent overwrite."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    with alice.session() as session:
        alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        session.commit()
        rows = session.exec(select(EventLog)).all()
        assert rows  # at least the project create
        assert all(row.base_rev is None for row in rows)  # genesis (unsigned harness)
