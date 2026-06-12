"""Telemetry service: default-off, registry enforcement, privacy, metrics (E28).

Domain (privacy) profile: the load-bearing assertions are the deny paths —
nothing records while opted out, unregistered events/props cannot record at
all, and ticket/memory content can never appear in a telemetry row.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.telemetry import (
    EVENTS,
    OPTIN_KEY,
    TelemetryError,
    TelemetryMetrics,
    TelemetryService,
)
from kantaq_db.meta import COLLECTION_META
from kantaq_db.models import AgentProposal, AuditEvent, EventLog, TelemetryEvent, Workspace
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _service(session: Session, clock: FakeClock | None = None) -> TelemetryService:
    if clock is None:
        return TelemetryService(session)
    return TelemetryService(session, now=lambda: clock.now().replace(tzinfo=None))


# --------------------------------------------------------------- default off


def test_default_is_off_and_record_is_a_noop(engine: Engine) -> None:
    with Session(engine) as session:
        service = _service(session)
        assert service.enabled() is False
        assert service.record("proposals_listed", {"count": 3}) is False
        session.commit()
        assert session.exec(select(TelemetryEvent)).all() == []


def test_optin_records_and_optout_stops(engine: Engine) -> None:
    with Session(engine) as session:
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        assert service.record("proposals_listed", {"count": 1}) is True
        service.set_enabled(False, actor_id="m1")
        assert service.record("proposals_listed", {"count": 2}) is False
        session.commit()
        rows = session.exec(select(TelemetryEvent)).all()
        assert [r.props for r in rows] == [{"count": 1}]


def test_toggle_writes_an_audit_row(engine: Engine) -> None:
    with Session(engine) as session:
        _service(session).set_enabled(True, actor_id="m1")
        session.commit()
        rows = session.exec(select(AuditEvent)).all()
        assert [r.action for r in rows] == ["telemetry.enable"]
        assert rows[0].object_ref == f"local_settings/{OPTIN_KEY}"
        assert rows[0].after == {"enabled": True}


def test_toggle_is_idempotent_no_duplicate_audit(engine: Engine) -> None:
    with Session(engine) as session:
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        service.set_enabled(True, actor_id="m1")  # no state change, no audit row
        session.commit()
        assert len(session.exec(select(AuditEvent)).all()) == 1


# ---------------------------------------------------------- registry (deny)


def test_unregistered_event_raises_even_when_enabled(engine: Engine) -> None:
    with Session(engine) as session:
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        with pytest.raises(TelemetryError, match="unregistered"):
            service.record("ticket_created", {"count": 1})


def test_unregistered_prop_key_raises_even_when_opted_out(engine: Engine) -> None:
    # Registry enforcement runs before the opt-in check so a capture-site bug
    # fails the suite even though tests rarely opt in.
    with Session(engine) as session, pytest.raises(TelemetryError, match="requires exactly"):
        _service(session).record("proposals_listed", {"title": "secret roadmap"})


def test_wrong_typed_prop_raises(engine: Engine) -> None:
    with Session(engine) as session, pytest.raises(TelemetryError, match="must be int"):
        _service(session).record("proposals_listed", {"count": "many"})


def test_free_text_cannot_pass_a_str_prop(engine: Engine) -> None:
    prose = "The auth refactor: rotate every member token before Friday's demo " * 3
    with Session(engine) as session, pytest.raises(TelemetryError, match="categorical bound"):
        _service(session).record("mcp_session_started", {"member_id": prose})


def test_bool_is_not_an_int(engine: Engine) -> None:
    with Session(engine) as session, pytest.raises(TelemetryError, match="must be int"):
        _service(session).record("proposals_listed", {"count": True})


def test_missing_props_raise_the_exact_set_is_required(engine: Engine) -> None:
    with Session(engine) as session, pytest.raises(TelemetryError, match="requires exactly"):
        _service(session).record("proposal_approved", {})


def test_retention_cap_prunes_the_oldest_rows(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SEC second review: a chatty producer bounds disk growth at the cap.
    import kantaq_core.telemetry as telemetry_module

    monkeypatch.setattr(telemetry_module, "RETENTION_MAX_ROWS", 3)
    with Session(engine) as session:
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        for n in range(6):
            service.record("proposals_listed", {"count": n})
            session.commit()
        rows = session.exec(select(TelemetryEvent)).all()
        assert len(rows) == 3
        assert sorted(r.props["count"] for r in rows) == [3, 4, 5]  # oldest pruned


# -------------------------------------------------------------- privacy pins


SENTINEL = "TOPSECRET-payroll-Q3-runway"


def test_no_ticket_or_memory_content_ever_lands_in_telemetry(engine: Engine) -> None:
    """FR-E28-1: with capture fully enabled, content stays unreachable.

    Records every registered event the way the capture sites do, with a
    sentinel-laden domain state in place, then string-scans the whole
    telemetry table dump for the sentinel.
    """
    with Session(engine) as session:
        workspace = Workspace(name=SENTINEL)
        session.add(workspace)
        session.flush()
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        service.record("proposal_approved", {"seconds_to_decision": 12.5})
        service.record("proposal_rejected", {"seconds_to_decision": 1.0})
        service.record("proposals_listed", {"count": 7})
        service.record("mcp_session_started", {"member_id": "01JCMEMBERULID0000000000"})
        service.record("activity_viewed", {"count": 2})
        session.commit()

        dump = json.dumps(
            [{"name": r.name, "props": r.props} for r in session.exec(select(TelemetryEvent)).all()]
        )
        assert SENTINEL not in dump
        assert len(json.loads(dump)) == len(EVENTS)


def test_telemetry_tables_are_not_syncable_collections(engine: Engine) -> None:
    """D-10: no sync path can pick telemetry up — it is not a collection."""
    assert "telemetry_events" not in COLLECTION_META
    assert "local_settings" not in COLLECTION_META
    with Session(engine) as session:
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        service.record("proposals_listed", {"count": 1})
        session.commit()
        synced_collections = {row.collection for row in session.exec(select(EventLog)).all()}
        assert "telemetry_events" not in synced_collections
        assert "local_settings" not in synced_collections


# ------------------------------------------------------------------- metrics


def test_metrics_fold_the_captured_events(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as session:
        service = _service(session, clock)
        service.set_enabled(True, actor_id="m1")
        service.record("proposal_approved", {"seconds_to_decision": 10.0})
        service.record("proposal_approved", {"seconds_to_decision": 30.0})
        service.record("proposal_rejected", {"seconds_to_decision": 20.0})
        service.record("mcp_session_started", {"member_id": "m-a"})
        service.record("mcp_session_started", {"member_id": "m-a"})
        service.record("mcp_session_started", {"member_id": "m-b"})
        service.record("activity_viewed", {"count": 4})
        session.commit()

        metrics = service.metrics()
        assert isinstance(metrics, TelemetryMetrics)
        assert metrics.enabled is True
        assert metrics.events_total == 7
        assert metrics.proposal_acceptance_rate == pytest.approx(2 / 3)
        assert metrics.median_seconds_to_approve == pytest.approx(20.0)
        assert metrics.mcp_sessions_total == 3
        assert metrics.repeat_session_members == 1  # m-a twice, m-b once
        assert metrics.activity_views_total == 1
        assert metrics.weekly_active is True


def test_metrics_on_an_empty_table_are_all_neutral(engine: Engine) -> None:
    with Session(engine) as session:
        metrics = _service(session).metrics()
        assert metrics.enabled is False
        assert metrics.events_total == 0
        assert metrics.proposal_acceptance_rate is None
        assert metrics.median_seconds_to_approve is None
        assert metrics.install_to_first_proposal_seconds is None
        assert metrics.weekly_active is False


def test_install_to_first_proposal_derives_from_domain_rows(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as session:
        workspace = Workspace(name="ws", created_at=clock.now().replace(tzinfo=None))
        session.add(workspace)
        session.flush()
        clock.advance(3600)
        session.add(
            AgentProposal(
                ticket_id="t-unchecked",
                proposer_id="agent-1",
                diff={},
                created_at=clock.now().replace(tzinfo=None),
            )
        )
        session.commit()
        metrics = _service(session, clock).metrics()
        assert metrics.install_to_first_proposal_seconds == pytest.approx(3600.0)


def test_weekly_active_goes_stale_after_seven_days(engine: Engine) -> None:
    clock = FakeClock()
    with Session(engine) as session:
        service = _service(session, clock)
        service.set_enabled(True, actor_id="m1")
        service.record("proposals_listed", {"count": 1})
        session.commit()
        assert service.metrics().weekly_active is True
        clock.advance(8 * 24 * 3600)
        assert service.metrics().weekly_active is False


def test_events_view_is_newest_first_and_bounded(engine: Engine) -> None:
    with Session(engine) as session:
        service = _service(session)
        service.set_enabled(True, actor_id="m1")
        for n in range(5):
            service.record("proposals_listed", {"count": n})
        session.commit()
        events = service.events(limit=3)
        assert len(events) == 3
        assert [e.props["count"] for e in events] == [4, 3, 2]
