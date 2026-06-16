"""MOD-07 aggregated agent reads (E07-T2, NFR-E07-2): reads roll up, not row-per-read."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core import audit
from kantaq_db import AuditEvent


@pytest.fixture
def session(temp_sqlite: Engine) -> Iterator[Session]:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        yield session


def test_many_reads_flush_to_one_summary_row(session: Session) -> None:
    log = audit.AgentReadLog()
    for _ in range(50):
        log.record("agent_bot", object_ref="tkt_1")
    assert log.pending == 50

    rows = log.flush(session)
    session.commit()

    assert len(rows) == 1
    stored = session.exec(select(AuditEvent)).one()
    assert stored.actor_id == "agent_bot"
    assert stored.action == audit.AGENT_READ_ACTION
    assert stored.source == "mcp"
    assert stored.after == {"reads": 50, "bytes": 0, "objects": {"tkt_1": 50}}


def test_reads_aggregate_per_agent_and_per_object(session: Session) -> None:
    log = audit.AgentReadLog()
    log.record("agent_a", object_ref="tkt_1")
    log.record("agent_a", object_ref="tkt_2")
    log.record("agent_a", object_ref="tkt_2")
    log.record("agent_b")  # a read with no object scope still counts

    rows = log.flush(session)
    session.commit()

    by_actor = {row.actor_id: row for row in rows}
    assert len(rows) == 2
    a = by_actor["agent_a"].after
    b = by_actor["agent_b"].after
    assert a is not None and a == {"reads": 3, "bytes": 0, "objects": {"tkt_1": 1, "tkt_2": 2}}
    assert b is not None and b == {"reads": 1, "bytes": 0, "objects": {}}


def test_payload_bytes_accumulate_into_the_summary(session: Session) -> None:
    """MOD-08: the gateway's per-read payload size rolls up to ``after.bytes``
    (the feed for metrics' ``est_payload_bytes``/``est_tokens``)."""
    log = audit.AgentReadLog()
    log.record("agent_bot", object_ref="tkt_1", payload_bytes=4096)
    log.record("agent_bot", object_ref="tkt_2", payload_bytes=900)
    log.record("agent_bot", object_ref="tkt_2")  # an unmeasured read adds 0

    rows = log.flush(session)
    session.commit()

    assert len(rows) == 1
    after = rows[0].after
    assert after is not None
    assert after == {"reads": 3, "bytes": 4996, "objects": {"tkt_1": 1, "tkt_2": 2}}


def test_negative_payload_bytes_is_refused(session: Session) -> None:
    log = audit.AgentReadLog()
    with pytest.raises(audit.AuditWriteError, match="non-negative"):
        log.record("agent_bot", payload_bytes=-1)


def test_flush_resets_the_tallies(session: Session) -> None:
    log = audit.AgentReadLog()
    log.record("agent_bot", object_ref="tkt_1")
    log.flush(session)

    assert log.pending == 0
    assert log.flush(session) == []
    session.commit()
    assert len(session.exec(select(AuditEvent)).all()) == 1


def test_a_read_must_be_attributed(session: Session) -> None:
    log = audit.AgentReadLog()
    with pytest.raises(audit.AuditWriteError, match="actor_id"):
        log.record("")


def test_concurrent_records_are_not_lost(session: Session) -> None:
    """The gateway records reads from many threads; the tally must not drop any."""
    import threading

    log = audit.AgentReadLog()

    def hammer(n: int) -> None:
        for i in range(500):
            log.record(f"agent_{n}", object_ref=f"tkt_{i % 3}")

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert log.pending == 2000
    rows = log.flush(session)
    session.commit()
    assert len(rows) == 4
    for row in rows:
        assert row.after is not None and row.after["reads"] == 500
