"""MOD-07 Merkle anchors (E07-T5, FR-E07-5): ``anchor_range`` + ``range_is_anchored``.

An anchor folds a range of this replica's audit trail into one RFC 6962 root over
the *same* canonical content the linear chain binds (``_chain_record``), so a
range proves itself with one root and a later re-chained forgery is caught. The
MOD-27 retention summarize gates on ``range_is_anchored`` before it blanks expired
detail. Here we anchor real ``audit.write`` rows, cross-check the root against the
primitive, prove every row by inclusion, and probe the refusals.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core import audit
from kantaq_core.audit import AuditWriteError, _chain_record
from kantaq_db import AuditEvent
from kantaq_protocol import (
    canonicalize,
    merkle_inclusion_proof,
    merkle_root,
    verify_inclusion_proof,
)
from kantaq_test_harness import FakeClock

ACTOR = "mbr_alice"


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _write_rows(session: Session, clock: FakeClock, n: int) -> list[AuditEvent]:
    rows: list[AuditEvent] = []
    for i in range(n):
        rows.append(
            audit.write(
                session,
                actor_id=ACTOR,
                action="ticket.update",
                source="mcp",
                object_ref=f"tkt_{i}",
                after={"n": i},
                now=clock.now(),
            )
        )
        clock.advance(1)  # a distinct created_at per row (ids are monotonic regardless)
    session.commit()
    return rows


def _leaves(rows: list[AuditEvent]) -> list[bytes]:
    return [canonicalize(_chain_record(r)) for r in rows]


def test_anchor_commits_the_range_to_a_merkle_root(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as s:
        rows = _write_rows(s, clock, 7)
        anchor = audit.anchor_range(s, actor_id=ACTOR)
        s.commit()
        assert anchor.tree_size == 7
        assert anchor.range_start == rows[0].id
        assert anchor.range_end == rows[-1].id
        assert anchor.chain_tip == rows[-1].chain_hash
        # The anchor's root is the primitive over the same bytes the chain binds.
        assert anchor.merkle_root == merkle_root(_leaves(rows))


def test_anchor_proves_every_row_by_inclusion(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as s:
        rows = _write_rows(s, clock, 9)
        anchor = audit.anchor_range(s, actor_id=ACTOR)
        leaves = _leaves(rows)
        for i in range(len(rows)):
            proof = merkle_inclusion_proof(leaves, i)
            assert verify_inclusion_proof(leaves[i], i, len(leaves), proof, anchor.merkle_root)


def test_range_is_anchored_reads_the_anchor_table(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as s:
        rows = _write_rows(s, clock, 5)
        assert audit.range_is_anchored(s, end_id=rows[-1].id) is False
        audit.anchor_range(s, actor_id=ACTOR, end_id=rows[-1].id)
        s.commit()
        assert audit.range_is_anchored(s, end_id=rows[-1].id) is True
        # A newer row beyond the anchored range is not yet covered.
        newer = _write_rows(s, clock, 1)
        assert audit.range_is_anchored(s, end_id=newer[-1].id) is False


def test_partial_range_anchors_only_its_prefix(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as s:
        rows = _write_rows(s, clock, 6)
        anchor = audit.anchor_range(s, actor_id=ACTOR, end_id=rows[2].id)
        s.commit()
        assert anchor.tree_size == 3
        assert anchor.range_end == rows[2].id


def test_empty_range_is_refused(engine: Engine) -> None:
    with Session(engine) as s, pytest.raises(AuditWriteError):
        audit.anchor_range(s, actor_id=ACTOR)


def test_an_unchained_row_is_refused(engine: Engine) -> None:
    """A row with no chain_hash (pre-v0.1 / DEBT-01) cannot be soundly anchored."""
    clock = FakeClock()
    with Session(engine) as s:
        # Insert a row WITHOUT write() so chain_hash stays NULL.
        s.add(
            AuditEvent(
                actor_id=ACTOR,
                action="legacy.row",
                source="app",
                created_at=clock.now(),
                updated_at=clock.now(),
            )
        )
        s.commit()
        with pytest.raises(AuditWriteError):
            audit.anchor_range(s, actor_id=ACTOR)


def test_external_pin_hook_records_an_attestation(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as s:
        _write_rows(s, clock, 3)
        anchor = audit.anchor_range(s, actor_id=ACTOR, pin=lambda root: f"pin:{root[:8]}")
        s.commit()
        assert anchor.external_pin is not None
        assert anchor.external_pin.startswith("pin:")


def test_unpinned_anchor_has_no_attestation(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as s:
        _write_rows(s, clock, 3)
        anchor = audit.anchor_range(s, actor_id=ACTOR)
        s.commit()
        assert anchor.external_pin is None
