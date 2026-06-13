"""MOD-07 hash chain (E07-T3, FR-E07-4): tamper-evident, survives migration.

The append-only guards (E07-T2) *refuse* app-layer UPDATE/DELETE; the hash
chain is the layer that makes the one path they cannot refuse — textual raw
SQL, below the app layer (DEBT-01) — *evident*. Every "attacker" here edits the
table the way an external sqlite3 writer would (``engine.begin()`` + ``text``),
which sails past the compiled-DML backstop, and then ``verify_chain`` names the
break. Verification reads in a fresh session, the way a real auditor would.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core import audit
from kantaq_core.audit import (
    CHAIN_EMPTY,
    CHAIN_OK,
    CHAIN_TAMPERED,
    CHAIN_TRUNCATED,
    CHAIN_UNCHAINED,
)
from kantaq_db import AuditEvent
from kantaq_protocol import HASH_HEX, SchemaViolation, chain_hash
from kantaq_test_harness import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _write_rows(
    session: Session, clock: FakeClock, n: int, *, actor: str = "mbr_alice"
) -> list[AuditEvent]:
    rows = []
    for i in range(n):
        rows.append(
            audit.write(
                session,
                actor_id=actor,
                action="ticket.update",
                source="app",
                object_ref=f"tkt_{i}",
                after={"status": "in_progress", "n": i},
                now=clock.now(),
            )
        )
        clock.advance(1)  # a distinct created_at per row (ids are monotonic regardless)
    session.commit()
    return rows


def _tamper(engine: Engine, sql: str, params: dict[str, object]) -> None:
    """Edit the table below the app layer, the way an external writer would."""
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _session(engine: Engine) -> Session:
    # expire_on_commit=False keeps the written rows readable after the session
    # closes, so a test can compare ids/hashes against a later verification pass.
    return Session(engine, expire_on_commit=False)


# --------------------------------------------------------------- write linking


def test_write_populates_a_strict_hex_chain_hash(engine: Engine, fake_clock: FakeClock) -> None:
    with _session(engine) as session:
        row = audit.write(
            session,
            actor_id="mbr_alice",
            action="ticket.create",
            source="app",
            now=fake_clock.now(),
        )
        session.commit()
        assert row.chain_hash is not None
        assert HASH_HEX.match(row.chain_hash) is not None


def test_genesis_then_each_row_links_to_its_predecessor(
    engine: Engine, fake_clock: FakeClock
) -> None:
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 3)
        # The first row is the genesis link (no predecessor); each later row
        # commits to the prior row's stored hash plus its own content.
        assert rows[0].chain_hash == chain_hash(None, audit._chain_record(rows[0]))
        assert rows[1].chain_hash == chain_hash(rows[0].chain_hash, audit._chain_record(rows[1]))
        assert rows[2].chain_hash == chain_hash(rows[1].chain_hash, audit._chain_record(rows[2]))


def test_links_survive_a_session_and_commit_boundary(engine: Engine, fake_clock: FakeClock) -> None:
    """A new session's first write links to the previously committed tip."""
    with _session(engine) as s1:
        _write_rows(s1, fake_clock, 2)
    with _session(engine) as s2:
        audit.write(
            s2, actor_id="mbr_bob", action="ticket.update", source="cli", now=fake_clock.now()
        )
        s2.commit()
    with _session(engine) as s3:
        assert audit.verify_chain(s3)  # spans both sessions


# --------------------------------------------------------------- clean verify


def test_verify_clean_chain_is_ok(engine: Engine, fake_clock: FakeClock) -> None:
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 5)
    with _session(engine) as session:
        result = audit.verify_chain(session)
        assert result.ok
        assert result.reason == CHAIN_OK
        assert result.event_id == rows[-1].id


def test_verify_empty_log_is_vacuously_ok(engine: Engine) -> None:
    with _session(engine) as session:
        result = audit.verify_chain(session)
        assert result.ok
        assert result.reason == CHAIN_EMPTY
        assert result.event_id is None


# --------------------------------------------------------------- tamper detect


def test_detects_a_below_app_layer_content_edit(engine: Engine, fake_clock: FakeClock) -> None:
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 4)
    target = rows[2].id

    _tamper(
        engine,
        "UPDATE audit_events SET action = :a WHERE id = :id",
        {"a": "ticket.delete", "id": target},
    )

    with _session(engine) as session:
        # The guards never saw it (raw SQL), but the chain does.
        assert session.get(AuditEvent, target).action == "ticket.delete"  # the edit landed
        result = audit.verify_chain(session)
        assert not result.ok
        assert result.reason == CHAIN_TAMPERED
        assert result.event_id == target


def test_detects_a_tampered_before_after_payload(engine: Engine, fake_clock: FakeClock) -> None:
    """The diff is bound, not just the id — editing ``after`` is evident."""
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 3)
    target = rows[1].id

    _tamper(
        engine,
        "UPDATE audit_events SET after = :a WHERE id = :id",
        {"a": '{"status": "done", "n": 99}', "id": target},
    )

    with _session(engine) as session:
        result = audit.verify_chain(session)
        assert result.reason == CHAIN_TAMPERED
        assert result.event_id == target


def test_detects_a_removed_interior_row(engine: Engine, fake_clock: FakeClock) -> None:
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 5)
    removed, next_row = rows[2].id, rows[3].id

    _tamper(engine, "DELETE FROM audit_events WHERE id = :id", {"id": removed})

    with _session(engine) as session:
        result = audit.verify_chain(session)
        assert result.reason == CHAIN_TAMPERED
        # The gap surfaces at the row whose predecessor vanished.
        assert result.event_id == next_row


def test_detects_a_removed_tail_row_against_an_anchor(
    engine: Engine, fake_clock: FakeClock
) -> None:
    """A truncation leaves the remaining chain self-consistent; the anchor catches it."""
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 4)
    expected_tip = rows[-1].chain_hash
    assert expected_tip is not None

    _tamper(engine, "DELETE FROM audit_events WHERE id = :id", {"id": rows[-1].id})

    with _session(engine) as session:
        # Without the anchor the shortened chain looks intact...
        assert audit.verify_chain(session).ok
        # ...but a verified range that must reach the known tip detects the loss.
        result = audit.verify_chain(session, expected_tip=expected_tip)
        assert not result.ok
        assert result.reason == CHAIN_TRUNCATED


def test_detects_a_forged_appended_row(engine: Engine, fake_clock: FakeClock) -> None:
    """A row spliced on below the app layer with a hash that doesn't link is caught.

    (Monotonic ULIDs are consecutive integers, so there is no id *between* two
    real rows to splice into — a forger appends past the tip; an appended row
    whose ``chain_hash`` doesn't chain off the real tip is evident.)
    """
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 3)
    forged_id = rows[-1].id[:10] + "Z" * 16  # max random tail → sorts after the tip
    assert forged_id > rows[-1].id

    _tamper(
        engine,
        "INSERT INTO audit_events (id, created_at, updated_at, actor_seq, visibility, "
        "hosting_mode, retention_policy, actor_id, action, source, chain_hash) VALUES "
        "(:id, :ts, :ts, 0, 'team', 'plain', 'standard', 'attacker', 'ticket.delete', 'app', :h)",
        {"id": forged_id, "ts": "2026-01-01 00:00:00.000000", "h": "f" * 64},
    )

    with _session(engine) as session:
        result = audit.verify_chain(session)
        assert result.reason == CHAIN_TAMPERED
        assert result.event_id == forged_id


def test_a_never_chained_row_is_reported_unchained(engine: Engine, fake_clock: FakeClock) -> None:
    """Pre-v0.1 rows (DEBT-01) carry no chain_hash; verification names them."""
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 2)
    _tamper(engine, "UPDATE audit_events SET chain_hash = NULL WHERE id = :id", {"id": rows[1].id})

    with _session(engine) as session:
        result = audit.verify_chain(session)
        assert result.reason == CHAIN_UNCHAINED
        assert result.event_id == rows[1].id


# --------------------------------------------------------------- ranged verify


def test_a_verified_range_trusts_its_boundary_anchor(engine: Engine, fake_clock: FakeClock) -> None:
    """Tampering before a range is invisible to that range, caught from genesis."""
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 5)
    _tamper(
        engine, "UPDATE audit_events SET action = :a WHERE id = :id", {"a": "x", "id": rows[0].id}
    )

    with _session(engine) as session:
        # Full verification from genesis catches the edit at row 0.
        full = audit.verify_chain(session)
        assert full.reason == CHAIN_TAMPERED
        assert full.event_id == rows[0].id
        # A sub-range starting at row 2 seeds from row 1's (untampered) hash and
        # holds — the boundary is the trust anchor of a "verified range".
        ranged = audit.verify_chain(session, start_id=rows[2].id)
        assert ranged.ok
        assert ranged.event_id == rows[-1].id


def test_a_range_detects_a_tamper_inside_it(engine: Engine, fake_clock: FakeClock) -> None:
    with _session(engine) as session:
        rows = _write_rows(session, fake_clock, 5)
    _tamper(
        engine, "UPDATE audit_events SET action = :a WHERE id = :id", {"a": "x", "id": rows[3].id}
    )

    with _session(engine) as session:
        result = audit.verify_chain(session, start_id=rows[2].id, end_id=rows[4].id)
        assert result.reason == CHAIN_TAMPERED
        assert result.event_id == rows[3].id


# --------------------------------------------------------------- write contract


def test_a_non_canonical_payload_is_refused_and_writes_nothing(engine: Engine) -> None:
    """before/after must be canonically encodable (no floats); fail before persist."""
    with _session(engine) as session:
        with pytest.raises(SchemaViolation):
            audit.write(
                session,
                actor_id="mbr_alice",
                action="ticket.update",
                source="app",
                after={"x": 0.5},
            )
        session.rollback()
        assert session.exec(select(AuditEvent)).first() is None


# --------------------------------------------------------------- migration


def test_chain_survives_a_migration_roundtrip(tmp_path: Path) -> None:
    """The chain is written into the existing column and survives schema moves.

    Build a chain on a migrated database, walk the migrations down to the first
    revision (which already holds ``audit_events`` + ``chain_hash``) and back to
    head — exercising every later migration over the data — and re-verify.
    """
    from kantaq_db import migrations, schema_version
    from kantaq_db.session import get_engine

    url = f"sqlite:///{tmp_path / 'chain.sqlite'}"
    clock = FakeClock()
    migrations.upgrade(url)
    db = get_engine(url)

    with _session(db) as session:
        rows = _write_rows(session, clock, 4)
    tip = rows[-1].chain_hash

    migrations.downgrade(url, "0001")
    assert schema_version.verify(db, expected=1).ok
    migrations.upgrade(url)
    assert schema_version.verify(db).ok

    with _session(db) as session:
        result = audit.verify_chain(session, expected_tip=tip)
        assert result.ok
        assert result.reason == CHAIN_OK
