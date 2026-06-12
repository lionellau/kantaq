"""TelemetryCapture honors the real service contract (MOD-30)."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.telemetry import TelemetryService
from kantaq_test_harness.telemetry import TelemetryCapture


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def test_capture_flips_the_real_optin_and_reads_back(engine: Engine) -> None:
    capture = TelemetryCapture(engine)
    assert capture.events() == []

    capture.enable()
    with Session(engine) as session:
        service = TelemetryService(session)
        assert service.enabled() is True
        assert service.record("proposals_listed", {"count": 1}) is True
        session.commit()
    assert capture.names() == ["proposals_listed"]

    capture.disable()
    with Session(engine) as session:
        assert TelemetryService(session).enabled() is False
        assert TelemetryService(session).record("proposals_listed", {"count": 2}) is False
    assert len(capture.events()) == 1
